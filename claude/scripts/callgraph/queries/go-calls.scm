;; Tree-sitter query: Go call-graph extraction

;; Function and method declarations
(function_declaration name: (identifier) @func.def)
(method_declaration  name: (field_identifier) @func.def)

;; Call sites — `foo(...)` and `pkg.Foo(...)` / `recv.Method(...)`
(call_expression
  function: [
    (identifier) @func.call
    (selector_expression field: (field_identifier) @func.call)
  ])
