/**
 * Vyapaar AI — Intro & Visual Controller
 * Handles intro launch, skip, keyboard ESC, and session persistence.
 */
(function() {
  const INTRO_CONTAINER_ID = 'vp-intro-container';
  const STORAGE_KEY = 'vy_intro_done';

  function initIntro() {
    const params = new URLSearchParams(window.location.search);
    const forceReplay = params.has('replay') || window.VYAPAAR_FORCE_REPLAY;
    const isDone = localStorage.getItem(STORAGE_KEY) === 'true';

    const container = document.getElementById(INTRO_CONTAINER_ID);
    if (!container) return;

    if (!forceReplay && isDone) {
      // Intro already seen: keep hidden
      container.style.display = 'none';
      return;
    }

    // Intro needs to play
    container.style.display = 'block';

    // Listen for completion from inside iframe or dispatched event
    window.addEventListener('message', function(e) {
      if (e.data && e.data.type === 'vyapaar-intro-done') {
        dismissIntro();
      }
    });

    window.addEventListener('vyapaar:intro-done', dismissIntro);

    // Escape key dismiss
    window.addEventListener('keydown', function(e) {
      if (e.key === 'Escape') {
        dismissIntro();
      }
    });
  }

  function dismissIntro() {
    const container = document.getElementById(INTRO_CONTAINER_ID);
    if (!container) return;

    localStorage.setItem(STORAGE_KEY, 'true');
    container.style.transition = 'opacity 0.35s ease';
    container.style.opacity = '0';

    // Notify backend
    try {
      fetch('/api/intro_done', { credentials: 'same-origin', method: 'POST' }).catch(function(){});
    } catch(e) {}

    setTimeout(function() {
      container.style.display = 'none';
      // Clean URL if replay was present
      if (window.location.search.includes('replay')) {
        const url = new URL(window.location.href);
        url.searchParams.delete('replay');
        window.history.replaceState({}, '', url.pathname + (url.searchParams.toString() ? '?' + url.searchParams.toString() : ''));
      }
    }, 360);
  }

  window.replayIntro = function() {
    localStorage.removeItem(STORAGE_KEY);
    window.location.href = window.location.pathname + '?replay=1';
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initIntro);
  } else {
    initIntro();
  }
})();
