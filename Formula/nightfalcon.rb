class Nightfalcon < Formula
  desc "Multi-client agentic security review harness"
  homepage "https://github.com/intuit/nightfalcon"
  url "https://github.com/intuit/nightfalcon/releases/download/v1.0.0/nightfalcon-1.0.0.pyz"
  version "1.0.0"
  sha256 "ef51ac0ed9c1becb5f4e725c517597504d2816cd8fa121594aff5142e39b24b4"
  license "Apache-2.0"

  depends_on "python@3.11"

  def install
    libexec.install "nightfalcon-1.0.0.pyz"
    (bin/"nightfalcon").write <<~EOS
      #!/bin/bash
      exec "#{Formula["python@3.11"].opt_bin}/python3.11" "#{libexec}/nightfalcon-1.0.0.pyz" "$@"
    EOS
  end

  test do
    assert_match "nightfalcon 1.0.0", shell_output("#{bin}/nightfalcon version")
    system "#{bin}/nightfalcon", "version"
  end
end
