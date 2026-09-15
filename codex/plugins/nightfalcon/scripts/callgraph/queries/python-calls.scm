;; Tree-sitter query: Python call-graph extraction

;; Function and method definitions
(function_definition name: (identifier) @func.def)

;; Call sites — bare `foo(...)`, `pkg.foo(...)`, `obj.method(...)`
(call
  function: [
    (identifier) @func.call
    (attribute attribute: (identifier) @func.call)
  ])
