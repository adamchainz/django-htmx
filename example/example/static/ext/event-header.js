(function() {
  function stringifyEvent(event) {
    var obj = {}
    for (var key in event) {
      obj[key] = event[key]
    }
    return JSON.stringify(obj, function(key, value) {
      if (value instanceof Node) {
        var nodeRep = value.tagName
        if (nodeRep) {
          nodeRep = nodeRep.toLowerCase()
          if (value.id) {
            nodeRep += '#' + value.id
          }
          if (value.classList && value.classList.length) {
            nodeRep += '.' + value.classList.toString().replace(' ', '.')
          }
          return nodeRep
        } else {
          return 'Node'
        }
      }
      if (value instanceof Window) return 'Window'
      return value
    })
  }

  // Ported from https://github.com/bigskysoftware/htmx-extensions/blob/main/src/event-header/event-header.js
  // for htmx 4's extension API, which has no upstream release yet.
  htmx.registerExtension('event-header', {
    htmx_config_request: function(elt, detail) {
      var sourceEvent = detail.ctx.sourceEvent
      if (sourceEvent) {
        detail.ctx.request.headers['Triggering-Event'] = stringifyEvent(sourceEvent)
      }
    }
  })
})()
