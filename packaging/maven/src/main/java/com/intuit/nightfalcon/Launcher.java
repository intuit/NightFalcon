package com.intuit.nightfalcon;

import java.io.IOException;
import java.io.InputStream;
import java.net.URISyntaxException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Enumeration;
import java.util.List;
import java.util.Map;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;

/** Offline launcher for embedded NightFalcon runtime. */
public final class Launcher {
    private static final String RUNTIME_PREFIX = "runtime/";
    private static final String EXECUTABLE_MANIFEST = "packaging/runtime-executables.txt";

    private Launcher() {}

    public static void main(String[] args) throws Exception {
        Path runtime = extractRuntime();
        try {
            String python = findPython();
            if (python == null) {
                System.err.println("nightfalcon: Python 3.11 or later is required");
                System.exit(1);
            }
            List<String> command = new ArrayList<>();
            command.add(python);
            command.add("-m");
            command.add("nightfalcon");
            command.addAll(Arrays.asList(args));
            ProcessBuilder builder = new ProcessBuilder(command).inheritIO();
            Map<String, String> environment = builder.environment();
            String pythonPath = runtime.resolve("src").toString();
            String existing = environment.get("PYTHONPATH");
            if (existing != null && !existing.isBlank()) {
                pythonPath += System.getProperty("path.separator") + existing;
            }
            environment.put("PYTHONPATH", pythonPath);
            int status = builder.start().waitFor();
            deleteTree(runtime);
            System.exit(status);
        } catch (Throwable failure) {
            deleteTree(runtime);
            throw failure;
        }
    }

    private static Path extractRuntime() throws IOException, URISyntaxException {
        Path jar = Path.of(Launcher.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        Path target = Files.createTempDirectory("nightfalcon-");
        if (!Files.isRegularFile(jar)) {
            Path source = jar.resolve(RUNTIME_PREFIX);
            copyTree(source, target);
            applyExecutablePermissions(target);
            return target;
        }
        try (JarFile archive = new JarFile(jar.toFile())) {
            Enumeration<JarEntry> entries = archive.entries();
            while (entries.hasMoreElements()) {
                JarEntry entry = entries.nextElement();
                if (!entry.getName().startsWith(RUNTIME_PREFIX) || entry.isDirectory()) {
                    continue;
                }
                String relative = entry.getName().substring(RUNTIME_PREFIX.length());
                Path output = target.resolve(relative).normalize();
                if (!output.startsWith(target)) {
                    throw new IOException("unsafe embedded runtime path: " + relative);
                }
                Files.createDirectories(output.getParent());
                try (InputStream input = archive.getInputStream(entry)) {
                    Files.copy(input, output, StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
        applyExecutablePermissions(target);
        return target;
    }

    private static void applyExecutablePermissions(Path root) throws IOException {
        Path manifest = root.resolve(EXECUTABLE_MANIFEST);
        for (String relative : Files.readAllLines(manifest)) {
            if (relative.isBlank()) {
                continue;
            }
            Path target = root.resolve(relative).normalize();
            if (!target.startsWith(root) || !Files.isRegularFile(target)) {
                throw new IOException("invalid executable payload path: " + relative);
            }
            try {
                Files.setPosixFilePermissions(target, PosixFilePermissions.fromString("rwx------"));
            } catch (UnsupportedOperationException ignored) {
                // Windows does not use POSIX executable bits.
            }
        }
    }

    private static String findPython() throws IOException, InterruptedException {
        String configured = System.getenv("NIGHTFALCON_PYTHON");
        List<String> candidates = new ArrayList<>();
        if (configured != null && !configured.isBlank()) {
            candidates.add(configured);
        }
        candidates.add("python3");
        candidates.add("python");
        for (String candidate : candidates) {
            try {
                Process process = new ProcessBuilder(candidate, "-c",
                        "import sys; raise SystemExit(sys.version_info < (3, 11))")
                        .redirectError(ProcessBuilder.Redirect.DISCARD)
                        .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                        .start();
                if (process.waitFor() == 0) {
                    return candidate;
                }
            } catch (IOException ignored) {
                // Try next candidate.
            }
        }
        return null;
    }

    private static void copyTree(Path source, Path target) throws IOException {
        try (var paths = Files.walk(source)) {
            for (Path path : paths.toList()) {
                Path output = target.resolve(source.relativize(path).toString()).normalize();
                if (!output.startsWith(target)) {
                    throw new IOException("unsafe runtime path");
                }
                if (Files.isDirectory(path)) {
                    Files.createDirectories(output);
                } else {
                    Files.createDirectories(output.getParent());
                    Files.copy(path, output, StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
    }

    private static void deleteTree(Path root) throws IOException {
        if (!Files.exists(root)) {
            return;
        }
        try (var paths = Files.walk(root)) {
            for (Path path : paths.sorted((left, right) -> right.compareTo(left)).toList()) {
                Files.deleteIfExists(path);
            }
        }
    }
}
