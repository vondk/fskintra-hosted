// Kopiér-knap til et felt med et link. Sidens eneste script: uden
// JavaScript bliver knappen ved med at være skjult, og linket kan stadig
// markeres og kopieres i hånden.
(function () {
  'use strict';

  function select(field) {
    field.focus();
    field.select();
    // select() alene markerer ikke altid hele feltet på iOS
    field.setSelectionRange(0, field.value.length);
  }

  function copy(field) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(field.value);
    }
    return new Promise(function (resolve, reject) {
      select(field);
      if (document.execCommand('copy')) {
        resolve();
      } else {
        reject(new Error('copy'));
      }
    });
  }

  function setup(button) {
    var field = document.getElementById(button.getAttribute('data-copy-from'));
    var status = document.getElementById(
      button.getAttribute('data-copy-status'));
    var label = button.textContent;
    var timer = null;
    if (!field) {
      return;
    }

    function say(short, long) {
      button.textContent = short;
      if (status) {
        status.textContent = long;
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        button.textContent = label;
        if (status) {
          status.textContent = '';
        }
      }, 2500);
    }

    function run() {
      copy(field).then(function () {
        say('Kopieret', 'Linket er kopieret.');
      }, function () {
        // Browseren sagde nej: markér linket, så det kan kopieres i hånden
        select(field);
        say('Markeret', 'Linket er markeret, men ikke kopieret.');
      });
    }

    button.hidden = false;
    button.addEventListener('click', run);
    // Et tryk på selve linket markerer det hele og kopierer det
    field.addEventListener('click', function () {
      select(field);
      run();
    });
  }

  var buttons = document.querySelectorAll('button[data-copy-from]');
  for (var i = 0; i < buttons.length; i++) {
    setup(buttons[i]);
  }
})();
