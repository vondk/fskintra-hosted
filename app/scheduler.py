"""Planlægger: kører alle aktive forældre sekventielt med jitter, én gang pr.
interval inden for RUN_HOURS. Første kørsel (catch-up) og "Kør nu" tages
inden for ~10 sekunder. Et fil-lås sikrer, at kun én planlægger kører."""

import datetime
import fcntl
import logging
import os
import random
import threading

from . import auth, jobs, tenants

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

    def tick(self, dt):
        conn = self.ctx.db()

        # 1) Ventende: første kørsel efter tilmelding og "Kør nu"
        pending = conn.execute(
            "SELECT id, needs_catchup FROM tenants WHERE status = 'active' "
            'AND (needs_catchup = 1 OR run_requested = 1) ORDER BY id'
        ).fetchall()
        for row in pending:
            if self.stop_event.is_set():
                return
            tenants.update(conn, row['id'], run_requested=0)
            jobs.execute(self.ctx, row['id'],
                         'catchup' if row['needs_catchup'] else 'run')

        # 2) Fast kørsel for alle aktive inden for tidsrummet
        slot = self._slot(dt)
        start, end = self.ctx.settings.run_hours
        if slot != self._last_slot and start <= dt.hour <= end:
            self._last_slot = slot
            self.cycle()
            auth.housekeeping(conn)

    def cycle(self):
        conn = self.ctx.db()
        ids = [r['id'] for r in conn.execute(
            "SELECT id FROM tenants WHERE status = 'active' AND "
            'needs_catchup = 0 ORDER BY last_run_at IS NOT NULL, last_run_at')]
        log.info('Kørsel af %d forældre', len(ids))
        for i, tid in enumerate(ids):
            if self.stop_event.is_set():
                return
            if i:
                self._sleep_jitter()
            jobs.execute(self.ctx, tid, 'run')
