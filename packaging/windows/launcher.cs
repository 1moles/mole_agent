// mole.exe：Windows 便携包的启动器，由 build.ps1 用系统自带的 .NET Framework 编译器 csc.exe 编译。
//
// 做的事只有一件：用包里自带的 python\python.exe 运行 mole_agent，参数原样转发，退出码原样返回。
//   -I        隔离模式：不读 PYTHONPATH / PYTHONHOME 等环境变量、不加用户 site-packages、
//             不把当前目录放进 sys.path（当前目录是用户的项目，里面的同名模块不能盖掉 Mole 自己的）
//   -X utf8   统一按 UTF-8 读写文件（-I 会忽略 PYTHONUTF8 环境变量，所以用命令行开关）
// mole_agent 所在的包根目录由 site-packages\mole_root.pth 加进 sys.path（相对路径，整个目录可以随意挪动）。
//
// Ctrl+C：同一个控制台里的进程都会收到。启动器自己忽略它，交给 Python 处理
// （Mole 用它打断当前任务、回到输入框），避免启动器先退出、把 Python 留在后台。
// 只用 C# 5 语法：Windows 自带的 csc.exe 只支持到 C# 5。

using System;
using System.Diagnostics;
using System.IO;
using System.Text;

internal static class MoleLauncher
{
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(Path.Combine(root, "python"), "python.exe");
        if (!File.Exists(python))
        {
            Console.Error.WriteLine("找不到 " + python);
            Console.Error.WriteLine("mole.exe 要和 python 目录放在一起，请重新解压完整的安装包。");
            return 9009;
        }

        StringBuilder commandLine = new StringBuilder("-I -X utf8 -m mole_agent");
        foreach (string arg in args)
        {
            commandLine.Append(' ');
            commandLine.Append(Quote(arg));
        }

        ProcessStartInfo info = new ProcessStartInfo(python, commandLine.ToString());
        info.UseShellExecute = false;   // 继承当前控制台和标准输入输出
        info.WorkingDirectory = Environment.CurrentDirectory;

        Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };

        try
        {
            using (Process process = Process.Start(info))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("启动 Python 失败：" + ex.Message);
            return 1;
        }
    }

    // 按 Windows 解析命令行的规则（CommandLineToArgvW）给参数加引号，Python 收到的参数和用户输入的一致
    private static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return arg;
        }
        StringBuilder quoted = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                quoted.Append('\\', backslashes * 2 + 1);
            }
            else
            {
                quoted.Append('\\', backslashes);
            }
            quoted.Append(c);
            backslashes = 0;
        }
        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }
}
