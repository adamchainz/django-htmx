{
  const data = document.currentScript.dataset;
  const isDebug = data.debug === "True";

  if (isDebug) {
    document.addEventListener("htmx:beforeOnLoad", function (event) {
      const xhr = event.detail.xhr;
      if (xhr.status == 500 || xhr.status == 404) {
        // Tell htmx to stop processing this response
        event.stopPropagation();

        document.children[0].innerHTML = xhr.response;

        // Run Django’s inline script
        // (1, eval) wtf - see https://stackoverflow.com/questions/9107240/1-evalthis-vs-evalthis-in-javascript
        (1, eval)(document.scripts[0].innerText);
        // Need to directly call Django’s onload function since browser won’t
        window.onload();
      }
    });
  }
}
