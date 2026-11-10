"""Planlægger: kører alle aktive forældre sekventielt med jitter, én gang pr.
interval inden for RUN_HOURS. Første kørsel (catch-up) og "Kør nu" tages
inden for ~10 sekunder; en fejlet catch-up prøves igen ved næste faste
slot. En fejl hos én forælder stopper aldrig de andre. Et fil-lås sikrer,
at kun én planlægger kører."""

import datetime
import fcntl
import logging
import os
import random
import threading

from . import aiproxy, auth, jobs, retention, tenants

log = logging.getLogger(__name__)

POLL_SECONDS = 10


class Scheduler(threading.Thread):
    def __init__(self, ctx, jitter=(5, 30)):
        super().__init__(name='scheduler', daemon=True)
        self.ctx = ctx
        self.jitter = jitter
        self.stop_event = threading.Event()
        self._last_slot = self._slot(datetime.datetime.now())
        self._lockfile = None

    def acquire_lock(self):
        path = os.path.join(self.ctx.settings.data_dir, 'scheduler.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lockfile = fd
        return True

    def _slot(self, dt):
        minutes = dt.hour * 60 + dt.minute
        return (dt.date(), minutes // self.ctx.settings.run_interval_minutes)

    def run(self):
        if not self.acquire_lock():
            log.warning('En anden planlægger kører allerede - stopper')
            return
        log.info('Planlægger startet')
        while not self.stop_event.is_set():
            try:
                self.tick(datetime.datetime.now())
            except Exception:  # planlæggeren må aldrig dø
                log.exception('Fejl i planlæggeren')
            self.stop_event.wait(POLL_SECONDS)

    def _sleep_jitter(self):
        lo, hi = self.jitter
        if hi > 0:
            # Kun jitter mellem kørsler - ikke til noget sikkerhedsrelateret
            self.stop_event.wait(random.uniform(lo, hi))  # nosec B311

    def _execute(self, tid, kind, clear_request=False):
        '''Kør én forælder. En exception må aldrig stoppe de andre: den
        logges (kun de første 8 tegn af id'et), og forælderen får
        last_run_at og fail_count opdateret, så den ikke låser rækkefølgen
        i cycle() fast'''
        conn = self.ctx.db()
        try:
            if clear_request:
                tenants.update(conn, tid, run_requested=0)
            jobs.execute(self.ctx, tid, kind)
        except Exception:
            log.exception('Kørsel fejlede tenant=%s type=%s', tid[:8], kind)
            try:
                tenants.record_failure(conn, tid)
            except Exception:  # fx disk fuld - fortsæt med de andre
                log.exception('Kunne ikke gemme fejlen for tenant=%s',
                              tid[:8])

    def tick(self, dt):
        conn = self.ctx.db()

        # 1) Ventende: første kørsel efter tilmelding og "Kør nu". En
        # catch-up der er fejlet (fail_count > 0) tages ikke her - ellers
        # prøves den igen hvert POLL_SECONDS - men ved næste faste slot
        pending = conn.execute(
            "SELECT id, needs_catchup FROM tenants WHERE status = 'active' "
            'AND ((needs_catchup = 1 AND fail_count = 0) OR '
            'run_requested = 1) ORDER BY id'
        ).fetchall()
        ran = set()
        for row in pending:
            if self.stop_event.is_set():
                return
            self._execute(row['id'],
                          'catchup' if row['needs_catchup'] else 'run',
                          clear_request=True)
            ran.add(row['id'])

        # 2) Fast kørsel for alle aktive inden for tidsrummet
        slot = self._slot(dt)
        start, end = self.ctx.settings.run_hours
        if slot != self._last_slot and start <= dt.hour <= end:
            self._last_slot = slot
            self.cycle(skip=ran)
            auth.housekeeping(conn)
            aiproxy.prune(conn)
            try:
                retention.sweep(self.ctx)
            except Exception:  # må ikke stoppe planlæggeren
                log.exception('Fejl i oprydningen')

    def cycle(self, skip=()):
        '''Kør alle aktive, længst siden først. Fejlede catch-ups prøves
        igen her (én gang pr. slot). skip: forældre der netop har kørt'''
        conn = self.ctx.db()
        rows = [r for r in conn.execute(
            "SELECT id, needs_catchup FROM tenants WHERE status = 'active' "
            'AND (needs_catchup = 0 OR fail_count > 0) '
            'ORDER BY last_run_at IS NOT NULL, last_run_at')
            if r['id'] not in skip]
        log.info('Kørsel af %d forældre', len(rows))
        for i, row in enumerate(rows):
            if self.stop_event.is_set():
                return
            if i:
                self._sleep_jitter()
            self._execute(row['id'],
                          'catchup' if row['needs_catchup'] else 'run')
