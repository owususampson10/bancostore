import "./main.css";
import Alpine from "alpinejs";
import "htmx.org";

window.Alpine = Alpine;
Alpine.start();

// Task 21d-iv: every prior htmx usage in this codebase was GET-only
// (filtering/pagination), so no CSRF wiring existed yet. Reads Django's
// own csrftoken cookie (CSRF_COOKIE_HTTPONLY defaults to False) rather
// than depending on a {% csrf_token %} form being present on whichever
// page triggers an htmx POST (mark notification read / mark all read).
function getCsrfCookie() {
  const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : null;
}

// CodeRabbit (PR #51): every htmx call in this codebase today targets a
// same-origin, relative {% url %} path, so this guard is currently a
// no-op in practice -- but attaching the token unconditionally would
// leak it to any future absolute/cross-origin htmx target, so the
// origin check stays even though nothing exercises the negative case
// yet.
function isSameOrigin(path) {
  try {
    return new URL(path, window.location.href).origin === window.location.origin;
  } catch (e) {
    return false;
  }
}

window.htmx.on("htmx:configRequest", (event) => {
  const token = getCsrfCookie();
  if (token && isSameOrigin(event.detail.path)) {
    event.detail.headers["X-CSRFToken"] = token;
  }
});
