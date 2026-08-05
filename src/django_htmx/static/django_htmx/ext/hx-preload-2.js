(function() {
  /**
   * This adds the "preload" extension to htmx. The extension will 
    * preload the targets of elements with "preload" attribute if:
    * - they also have `href`, `hx-get` or `data-hx-get` attributes
    * - they are radio buttons, checkboxes, select elements and submit
    *   buttons of forms with `method="get"` or `hx-get` attributes
    * The extension relies on browser cache and for it to work
    * server response must include `Cache-Control` header
    * e.g. `Cache-Control: private, max-age=60`.
    * For more details @see https://htmx.org/extensions/preload/
  */

  htmx.defineExtension('preload', {
    onEvent: function(name, event) {
      // Process preload attributes on `htmx:afterProcessNode`
      if (name === 'htmx:afterProcessNode') {
        // Initialize all nodes with `preload` attribute
        const parent = event.target || event.detail.elt;
        const preloadNodes = [
          ...parent.hasAttribute("preload") ? [parent] : [],
          ...parent.querySelectorAll("[preload]")]
        preloadNodes.forEach(function(node) {
          // Initialize the node with the `preload` attribute
          init(node)

          // Initialize all child elements which has
          // `href`, `hx-get` or `data-hx-get` attributes
          node.querySelectorAll('[href],[hx-get],[data-hx-get]').forEach(init)
        })
        return
      }

      // Intercept HTMX preload requests on `htmx:beforeRequest` and
      // send them as XHR requests instead to avoid side-effects,
      // such as showing loading indicators while preloading data. 
      if (name === 'htmx:beforeRequest') {
        const requestHeaders = event.detail.requestConfig.headers
        if (!("HX-Preloaded" in requestHeaders
              && requestHeaders["HX-Preloaded"] === "true")) {
          return
        }

        event.preventDefault()
        // Reuse XHR created by HTMX with replaced callbacks
        const xhr = event.detail.xhr
        xhr.onload = function() {
          processResponse(event.detail.elt, xhr.responseText)
        }
        xhr.onerror = null
        xhr.onabort = null
        xhr.ontimeout = null
        xhr.send()
      }
    }
  })

  /**
   * Initialize `node`, set up event handlers based on own or inherited
   * `preload` attributes and set `node.preloadState` to `READY`.
   * 
   * `node.preloadState` can have these values:
   * - `READY` - event handlers have been set up and node is ready to preload
   * - `TIMEOUT` - a triggering event has been fired, but `node` is not
   *   yet being loaded because some time need to pass first e.g. user
   *   has to keep hovering over an element for 100ms for preload to start
   * - `LOADING` means that `node` is in the process of being preloaded
   * - `DONE` means that the preloading process is complete and `node`
   *    doesn't need a repeated preload (indicated by preload="always")
   * @param {Node} node
   */
  function init(node) {
    // Guarantee that each node is initialized only once
    if (node.preloadState !== undefined) {
      return
    }

    if (!isValidNodeForPreloading(node)) {
      return
    }

    // Initialize form element preloading
    if (node instanceof HTMLFormElement) {
      const form = node
      // Only initialize forms with `method="get"` or `hx-get` attributes
      if (!((form.hasAttribute('method') && form.method === 'get')
        || form.hasAttribute('hx-get') || form.hasAttribute('hx-data-get'))) {
        return
      }
      for (let i = 0; i < form.elements.length; i++) {
        const element = form.elements.item(i);
        init(element);
        if ("labels" in element) {
          element.labels.forEach(init);
        }
      }
      return
    }
    
    // Process node configuration from preload attribute
    let preloadAttr = getClosestAttribute(node, 'preload');
    node.preloadAlways = preloadAttr && preloadAttr.includes('always');
    if (node.preloadAlways) {
      preloadAttr = preloadAttr.replace('always', '').trim();
    }
    let triggerEventName = preloadAttr || 'mousedown';

    // Set up event handlers listening for triggering events
    const needsTimeout = triggerEventName === 'mouseover'
    node.addEventListener(triggerEventName, getEventHandler(node, needsTimeout), {passive: true})

    // Add `touchstart` listener for touchscreen support
    // if `mousedown` or `mouseover` is used
    if (triggerEventName === 'mousedown' || triggerEventName === 'mouseover') {
      node.addEventListener('touchstart', getEventHandler(node), {passive: true})
    }

    // If `mouseover` is used, set up `mouseout` listener,
    // which will abort preloading if user moves mouse outside
    // the element in less than 100ms after hovering over it
    if (triggerEventName === 'mouseover') {
      node.addEventListener('mouseout', function(evt) {
        if ((evt.target === node) && (node.preloadState === 'TIMEOUT')) {
          node.preloadState = 'READY'
        }
      }, {passive: true})
    }

    // Mark the node as ready to be preloaded
    node.preloadState = 'READY'

    // This event can be used to load content immediately
    htmx.trigger(node, 'preload:init')
  }

  /**
   * Return event handler which can be called by event listener to start
   * the preloading process of `node` with or without a timeout
   * @param {Node} node 
   * @param {boolean=} needsTimeout 
   * @returns {function(): void}
   */
  function getEventHandler(node, needsTimeout = false) {
    return function() {
      // Do not preload uninitialized nodes, nodes which are in process
      // of being preloaded or have been preloaded and don't need repeat
      if (node.preloadState !== 'READY') {
        return
      }

      if (needsTimeout) {
        node.preloadState = 'TIMEOUT'
        const timeoutMs = 100
        window.setTimeout(function() {
          if (node.preloadState === 'TIMEOUT') {
            node.preloadState = 'READY'
            load(node)
          }
        }, timeoutMs)
        return
      }

      load(node)
    }
  }

  /**
   * Preload the target of node, which can be:
   *  - hx-get or data-hx-get attribute
   *  - href or form action attribute
   * @param {Node} node 
   */
  function load(node) {
    // Do not preload uninitialized nodes, nodes which are in process
    // of being preloaded or have been preloaded and don't need repeat
    if (node.preloadState !== 'READY') {
      return
    }
    node.preloadState = 'LOADING'

    // Load nodes with `hx-get` or `data-hx-get` attribute
    // Forms don't reach this because only their elements are initialized
    const hxGet = node.getAttribute('hx-get') || node.getAttribute('data-hx-get')
    if (hxGet) {
      sendHxGetRequest(hxGet, node);
      return
    }

    // Load nodes with `href` attribute
    const hxBoost = getClosestAttribute(node, "hx-boost") === "true"
    if (node.hasAttribute('href')) {
      const url = node.getAttribute('href');
      if (hxBoost) {
        sendHxGetRequest(url, node);
      } else {
        sendXmlGetRequest(url, node);
      }
      return
    }

    // Load form elements
    if (isPreloadableFormElement(node)) {
      const url = node.form.getAttribute('action')
                  || node.form.getAttribute('hx-get')
                  || node.form.getAttribute('data-hx-get');
      const formData = htmx.values(node.form);
      const isStandardForm = !(node.form.getAttribute('hx-get')
                              || node.form.getAttribute('data-hx-get')
                              || hxBoost);
      const sendGetRequest = isStandardForm ? sendXmlGetRequest : sendHxGetRequest

      // submit button
      if (node.type === 'submit') {
        sendGetRequest(url, node.form, formData)
        return
      }
      
      // select
      const inputName = node.name || node.control.name;
      if (node.tagName === 'SELECT') {
        Array.from(node.options).forEach(option => {
          if (option.selected) return;
          formData.set(inputName, option.value);
          const formDataOrdered = forceFormDataInOrder(node.form, formData);
          sendGetRequest(url, node.form, formDataOrdered)
        });
        return
      }

      // radio and checkbox
      const inputType = node.getAttribute("type") || node.control.getAttribute("type");
      const nodeValue = node.value || node.control?.value;
      if (inputType === 'radio') {
        formData.set(inputName, nodeValue);
      } else if (inputType === 'checkbox'){
        const inputValues = formData.getAll(inputName);
        if (inputValues.includes(nodeValue)) {
          formData[inputName] = inputValues.filter(value => value !== nodeValue);
        } else {
          formData.append(inputName, nodeValue);
        }
      }
      const formDataOrdered = forceFormDataInOrder(node.form, formData);
      sendGetRequest(url, node.form, formDataOrdered)
      return
    }
  }

  /**
   * Force formData values to be in the order of form elements.
   * This is useful to apply after alternating formData values
   * and before passing them to a HTTP request because cache is
   * sensitive to GET parameter order e.g., cached `/link?a=1&b=2`
   * will not be used for `/link?b=2&a=1`.
   * @param {HTMLFormElement} form 
   * @param {FormData} formData 
   * @returns {FormData}
   */
  function forceFormDataInOrder(form, formData) {
    const formElements = form.elements;
    const orderedFormData = new FormData();
    for(let i = 0; i < formElements.length; i++) {
      const element = formElements.item(i);
      if (formData.has(element.name) && element.tagName === 'SELECT') {
        orderedFormData.append(
          element.name, formData.get(element.name));
        continue;
      }
      if (formData.has(element.name) && formData.getAll(element.name)
        .includes(element.value)) {
        orderedFormData.append(element.name, element.value);
      }
    }
    return orderedFormData;
  }

  /**
   * Send GET request with `hx-request` headers as if `sourceNode`
   * target was loaded. Send alternated values if `formData` is set.
   * 
   * Note that this request is intercepted and sent as XMLHttpRequest.
   * It is necessary to use `htmx.ajax` to acquire correct headers which
   * HTMX and extensions add based on `sourceNode`. But it cannot be used
   * to perform the request due to side-effects e.g. loading indicators. 
   * @param {string} url 
   * @param {Node} sourceNode 
   * @param {FormData=} formData
   */
  function sendHxGetRequest(url, sourceNode, formData = undefined) {
    htmx.ajax('GET', url, {
      source: sourceNode,
      values: formData,
      headers: {"HX-Preloaded": "true"}
    });
  }

  /**
   * Send XML GET request to `url`. Send `formData` as URL params if set.
   * @param {string} url
   * @param {Node} sourceNode
   * @param {FormData=} formData
   */
  function sendXmlGetRequest(url, sourceNode, formData = undefined) {
    const xhr = new XMLHttpRequest()
    if (formData) {
      url += '?' + new URLSearchParams(formData.entries()).toString()
    }
    xhr.open('GET', url);
    xhr.setRequestHeader("HX-Preloaded", "true")
    xhr.onload = function() { processResponse(sourceNode, xhr.responseText) }
    xhr.send()
  }

  /**
   * Process request response by marking node `DONE` to prevent repeated
   * requests, except if preload attribute contains `always`,
   * and load linked resources (e.g. images) returned in the response 
   * if `preload-images` attribute is `true`
   * @param {Node} node 
   * @param {string} responseText 
   */
  function processResponse(node, responseText) {
    node.preloadState = node.preloadAlways ? 'READY' : 'DONE'

    if (getClosestAttribute(node, 'preload-images') === 'true') {
      // Load linked resources
      document.createElement('div').innerHTML = responseText
    }
  }

  /**
   * Gets attribute value from node or one of its parents
   * @param {Node} node 
   * @param {string} attribute 
   * @returns { string | undefined }
   */
  function getClosestAttribute(node, attribute) {
    if (node == undefined) { return undefined }
    return node.getAttribute(attribute)
      || node.getAttribute('data-' + attribute)
      || getClosestAttribute(node.parentElement, attribute)
  }

  /**
   * Determines if node is valid for preloading and should be
   * initialized by setting up event listeners and handlers
   * @param {Node} node 
   * @returns {boolean}
   */
  function isValidNodeForPreloading(node) {
    // Add listeners only to nodes which include "GET" transactions
    // or preloadable "GET" form elements
    const getReqAttrs = ['href', 'hx-get', 'data-hx-get'];
    const includesGetRequest = node => getReqAttrs.some(a => node.hasAttribute(a))
                                        || node.method === 'get';
    const isPreloadableGetFormElement = node.form instanceof HTMLFormElement
                                        && includesGetRequest(node.form)
                                        && isPreloadableFormElement(node)
    if (!includesGetRequest(node) && !isPreloadableGetFormElement) {
      return false
    }

    // Don't preload <input> elements contained in <label>
    // to prevent sending two requests. Interaction on <input> in a
    // <label><input></input></label> situation activates <label> too.
    if (node instanceof HTMLInputElement && node.closest('label')) {
      return false
    }

    return true
  }

  /**
   * Determine if node is a form element which can be preloaded,
   * i.e., `radio`, `checkbox`, `select` or `submit` button
   * or a `label` of a form element which can be preloaded.
   * @param {Node} node 
   * @returns {boolean}
   */
  function isPreloadableFormElement(node) {
    if (node instanceof HTMLInputElement || node instanceof HTMLButtonElement) {
      const type = node.getAttribute('type');
      return ['checkbox', 'radio', 'submit'].includes(type);
    }
    if (node instanceof HTMLLabelElement) {
      return node.control && isPreloadableFormElement(node.control);
    }
    return node instanceof HTMLSelectElement;
  }
})()
