{
  const data = document.currentScript.dataset;
  const isDebug = data.debug === "True";

  if (isDebug) {
    function showErrorResponse(html) {
      document.children[0].innerHTML = html;

      // Run inline scripts, which Django’s error pages use
      for (const script of document.scripts) {
        // (1, eval) wtf - see https://stackoverflow.com/questions/9107240/1-evalthis-vs-evalthis-in-javascript
        (1, eval)(script.innerText);
      }

      // Run window.onload function if defined, which Django’s error pages use
      if (typeof window.onload === "function") {
        window.onload();
      }
    }

    const isHtmx4 = window.htmx && window.htmx.version && window.htmx.version.startsWith("4.");

    if (isHtmx4) {
      // htmx 4 swaps error responses into their request's target by default,
      // so opt back out of that and handle them ourselves instead.
      htmx.config.noSwap.push("4xx", "5xx");

      document.addEventListener("htmx:response:error", function (event) {
        const status = event.detail.ctx.response.status;
        if (status == 400 || status == 403 || status == 404 || status == 500) {
          showErrorResponse(event.detail.ctx.text);
        }
      });
    } else {
      document.addEventListener("htmx:beforeOnLoad", function (event) {
        const xhr = event.detail.xhr;
        if (xhr.status == 400 || xhr.status == 403 || xhr.status == 404 || xhr.status == 500 ) {
          // Tell htmx to stop processing this response
          event.stopPropagation();

          showErrorResponse(xhr.response);
        }
      });
    }
  }
}
