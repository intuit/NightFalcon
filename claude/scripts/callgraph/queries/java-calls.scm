;; Tree-sitter query: Java call-graph extraction
;; Captures every method definition and every method invocation by name.
;; Calls are resolved by name only — the LLM disambiguates when needed.

;; Method declarations
(method_declaration
  name: (identifier) @method.def)

;; Constructor declarations (relevant for `new Foo(...)` taint sources/sinks)
(constructor_declaration
  name: (identifier) @method.def)

;; Method invocations: `foo(...)` and `obj.foo(...)`
(method_invocation
  name: (identifier) @method.call)

;; `new Foo(...)` — captures the class being constructed
(object_creation_expression
  type: (type_identifier) @method.call)
