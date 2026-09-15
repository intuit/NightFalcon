using System.Diagnostics;

static string? FindPython()
{
    var configured = Environment.GetEnvironmentVariable("NIGHTFALCON_PYTHON");
    var candidates = new[] { configured, "python3", "python" };
    foreach (var candidate in candidates.Where(value => !string.IsNullOrWhiteSpace(value)))
    {
        try
        {
            using var probe = Process.Start(new ProcessStartInfo
            {
                FileName = candidate!,
                ArgumentList = { "-c", "import sys; raise SystemExit(sys.version_info < (3, 11))" },
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            });
            probe!.WaitForExit();
            if (probe.ExitCode == 0)
            {
                return candidate;
            }
        }
        catch
        {
            // Try next candidate.
        }
    }
    return null;
}

static void ApplyExecutablePermissions(string runtime)
{
    if (OperatingSystem.IsWindows())
    {
        return;
    }
    var manifest = Path.Combine(runtime, "packaging", "runtime-executables.txt");
    foreach (var relative in File.ReadLines(manifest))
    {
        if (string.IsNullOrWhiteSpace(relative))
        {
            continue;
        }
        var target = Path.GetFullPath(Path.Combine(runtime, relative));
        if (!target.StartsWith(Path.GetFullPath(runtime) + Path.DirectorySeparatorChar, StringComparison.Ordinal)
            || !File.Exists(target))
        {
            throw new InvalidDataException($"invalid executable payload path: {relative}");
        }
        File.SetUnixFileMode(target, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
    }
}

var python = FindPython();
if (python is null)
{
    Console.Error.WriteLine("nightfalcon: Python 3.11 or later is required");
    return 1;
}

var runtime = Path.Combine(AppContext.BaseDirectory, "runtime");
var source = Path.Combine(runtime, "src");
if (!Directory.Exists(Path.Combine(source, "nightfalcon")))
{
    Console.Error.WriteLine("nightfalcon: embedded src/nightfalcon runtime is missing");
    return 1;
}
ApplyExecutablePermissions(runtime);

var start = new ProcessStartInfo
{
    FileName = python,
    UseShellExecute = false,
};
start.ArgumentList.Add("-m");
start.ArgumentList.Add("nightfalcon");
foreach (var argument in args)
{
    start.ArgumentList.Add(argument);
}
var currentPythonPath = Environment.GetEnvironmentVariable("PYTHONPATH");
start.Environment["PYTHONPATH"] = string.IsNullOrEmpty(currentPythonPath)
    ? source
    : source + Path.PathSeparator + currentPythonPath;
using var process = Process.Start(start);
process!.WaitForExit();
return process.ExitCode;
