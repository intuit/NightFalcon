# frozen_string_literal: true

Gem::Specification.new do |spec|
  spec.name = "nightfalcon"
  spec.version = "3.0.0"
  spec.summary = "Multi-client agentic security review harness"
  spec.description = "Installs and verifies NightFalcon harnesses for Claude Code, Codex, and Cursor."
  spec.authors = ["NightFalcon contributors"]
  spec.homepage = "https://github.com/intuit/nightfalcon"
  spec.license = "Apache-2.0"
  spec.required_ruby_version = Gem::Requirement.new(">= 2.6")
  spec.metadata = {
    "source_code_uri" => "https://github.com/intuit/nightfalcon",
    "bug_tracker_uri" => "https://github.com/intuit/nightfalcon/issues"
  }
  allowed = %r{\A(?:src/nightfalcon|claude|codex|cursor|packaging/rubygems/bin)/}
  fixed = %w[LICENSE NOTICE README.md VERSION]
  spec.files = IO.popen(%w[git ls-files -z], &:read).split("\0").select do |path|
    path.match?(allowed) || fixed.include?(path)
  end
  spec.bindir = "packaging/rubygems/bin"
  spec.executables = ["nightfalcon"]
  spec.require_paths = ["src"]
end
