// qemu-system-x86_64.exe shim for cua on Windows (diagnostic build).
// Rewrites `-cpu host` -> `-cpu qemu64` (WHPX crash fix), forwards everything
// to <shimname>.real.exe, kill-on-close job object, and logs diagnostics:
// cwd, key env vars, and test-opens of the VNC keymap file.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public static class QemuShim {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr CreateJobObject(IntPtr sa, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool SetInformationJobObject(IntPtr job, int cls, ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION info, int size);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public long PerProcessUserTimeLimit; public long PerJobUserTimeLimit;
        public uint LimitFlags; public UIntPtr MinimumWorkingSetSize; public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass; public uint SchedulingClass;
        public UIntPtr ProcessMemoryLimit; public UIntPtr JobMemoryLimit; public UIntPtr PeakProcessMemoryUsed; public UIntPtr PeakJobMemoryUsed;
    }
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

    static void Probe(string label, string path, StringBuilder sb) {
        IntPtr h = CreateFile(path, 0x80000000 /*GENERIC_READ*/, 1 /*share read*/, IntPtr.Zero,
                              3 /*OPEN_EXISTING*/, 0x80 /*FILE_ATTRIBUTE_NORMAL*/, IntPtr.Zero);
        if (h == new IntPtr(-1)) {
            int err = Marshal.GetLastWin32Error();
            sb.AppendLine(label + " -> FAIL winerr=" + err + " (" + new System.ComponentModel.Win32Exception(err).Message + ")");
        } else {
            CloseHandle(h);
            sb.AppendLine(label + " -> ok");
        }
    }

    public static int Main(string[] args) {
        string me = Process.GetCurrentProcess().MainModule.FileName;
        string dir = Path.GetDirectoryName(me);
        string real = Path.Combine(dir, Path.GetFileNameWithoutExtension(me) + ".real.exe");
        var log = new StringBuilder();
        log.AppendLine("=== qemu shim " + DateTime.Now.ToString("o") + " pid=" + Process.GetCurrentProcess().Id);
        log.AppendLine("exe=" + me);
        log.AppendLine("cwd=" + Directory.GetCurrentDirectory());
        foreach (string v in new string[] { "LANG", "LC_ALL", "LC_CTYPE", "TEMP", "TMP", "USERPROFILE", "HOME", "QEMU_DATA_DIR", "XKB_CONFIG_ROOT" }) {
            string val = Environment.GetEnvironmentVariable(v);
            log.AppendLine("env " + v + "=" + (val ?? "(null)"));
        }
        log.AppendLine("argv=" + string.Join(" ", Array.ConvertAll(args, a => a.Length > 90 ? a.Substring(0, 90) + "…" : a)));
        Probe("open en-us (cwd-relative)", "en-us", log);
        Probe("open datadir keymap", Path.Combine(dir, "share", "qemu", "keymaps", "en-us"), log);
        try {
            File.AppendAllText(Path.Combine(Path.GetTempPath(), "qemu-shim-debug.log"), log.ToString());
        } catch { }

        if (!File.Exists(real)) {
            Console.Error.WriteLine("qemu shim: missing " + real);
            return 127;
        }
        for (int i = 0; i + 1 < args.Length; i++) {
            if (args[i] == "-cpu" && args[i + 1] == "host") args[i + 1] = "qemu64";
        }
        var psi = new ProcessStartInfo {
            FileName = real, UseShellExecute = false,
            Arguments = string.Join(" ", Array.ConvertAll(args, Quote)),
            // cua's daemon spawns qemu with CWD=C:\WINDOWS\system32, where the
            // VNC keymap probe ("en-us") fails with EACCES and QEMU aborts
            // instead of falling through to its data dir. A neutral CWD fixes it.
            WorkingDirectory = dir,
        };
        var job = CreateJobObject(IntPtr.Zero, null);
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job, 9, ref info, Marshal.SizeOf(info));
        var p = Process.Start(psi);
        if (p == null) return 126;
        AssignProcessToJobObject(job, p.Handle);
        p.WaitForExit();
        return p.ExitCode;
    }

    static string Quote(string a) {
        if (a.Length == 0) return "\"\"";
        if (a.IndexOfAny(new char[] { ' ', '\t', '"' }) < 0) return a;
        return "\"" + a.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }
}
