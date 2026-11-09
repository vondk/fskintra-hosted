/* Viser tydeligt, at en formular er sendt, og forhindrer at den sendes
   igen, mens svaret er på vej (login-mails og login-tjek kan tage flere
   sekunder). Kun en forbedring: uden JavaScript virker siderne som før.

   Den trykkede knap får .is-busy (spinner i style.css) og evt. teksten fra
   data-busy. Knappen gøres ikke disabled: så ville dens name/value ikke
   komme med i formularen */
(function () {
  'use strict';

  // Efter gunicorns timeout er svaret ikke længere på vej, så man skal
  // kunne prøve igen
  var RESET_MS = 120000;

  function submitter(form, e) {
    return e.submitter ||
      form.querySelector('button[type=submit], button:not([type])');
  }

  function reset(form) {
    clearTimeout(form.busyTimer);
    form.removeAttribute('aria-busy');
    var b = form.querySelector('.is-busy');
    if (!b) return;
    b.classList.remove('is-busy');
    b.removeAttribute('aria-disabled');
    if (b.hasAttribute('data-idle')) {
      b.textContent = b.getAttribute('data-idle');
      b.removeAttribute('data-idle');
    }
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.method !== 'post') return;
    if (form.getAttribute('aria-busy') === 'true') {
      e.preventDefault();
      return;
    }
    form.setAttribute('aria-busy', 'true');
    var b = submitter(form, e);
    if (b) {
      b.classList.add('is-busy');
      b.setAttribute('aria-disabled', 'true');
      var text = b.getAttribute('data-busy');
      if (text) {
        b.setAttribute('data-idle', b.textContent);
        b.textContent = text;
      }
    }
    form.busyTimer = setTimeout(function () { reset(form); }, RESET_MS);
  });

  // Tilbage-knappen kan vise siden fra bfcache med knappen stadig i gang
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) {
      Array.prototype.forEach.call(
        document.querySelectorAll('form[aria-busy]'), reset);
    }
  });
})();
