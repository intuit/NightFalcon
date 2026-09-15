;; Tree-sitter query: JavaScript / TypeScript call-graph extraction
;; (Use with the tree-sitter-javascript grammar; TS support via .tsx/.ts may
;; require tree-sitter-typescript; the JS grammar handles plain JS and JSX.)

;; Function declarations
(function_declaration name: (identifier) @func.def)

;; Function expressions assigned to const/let/var or as object properties
(variable_declarator
  name: (identifier) @func.def
  value: [(function_expression) (arrow_function)])

(pair
  key: (property_identifier) @func.def
  value: [(function_expression) (arrow_function)])

;; Class methods
(method_definition name: (property_identifier) @func.def)

;; Call sites: `foo(...)` and `obj.foo(...)`
(call_expression
  function: [
    (identifier) @func.call
    (member_expression property: (property_identifier) @func.call)
  ])

;; `new Foo(...)` — class instantiation
(new_expression
  constructor: [
    (identifier) @func.call
    (member_expression property: (property_identifier) @func.call)
  ])
