Mole 便携版（Windows 10/11，64 位）
====================================

自带 Python，电脑上不需要安装 Python。

一、安装
1. 把 zip 解压到一个你有写权限的目录，例如 D:\tools\mole-windows-x64。
   不要放在 C:\Program Files 下面（没有管理员权限写不了配置文件）。
   路径尽量短一些，Windows 对过长的路径有限制。
2. 在这个目录里打开 PowerShell（在资源管理器地址栏输入 powershell 回车），执行一次：
       powershell -ExecutionPolicy Bypass -File .\install.ps1
   它会把目录加入当前用户的 PATH，并生成 .env 文件。不需要管理员权限。
3. 用记事本打开同目录的 .env，填上你要用的供应商的 API key。
   供应商、地址、模型列表在同目录的 models.toml 里改。
4. 重新打开 PowerShell，执行：
       mole --check
   看到「模型可用」就可以了。

不想改 PATH 的话，跳过第 2 步，直接运行 D:\tools\mole-windows-x64\mole.exe；
.env 自己从 .env.example 复制一份。

二、使用
    cd D:\code\your-project
    mole                         进入对话
    mole -p "解释一下这个仓库"     执行一条指令后退出
    mole -c                      继续这个项目最近的会话
    mole --list-models           看看有哪些模型可用
在对话里输入 / 可以看到全部命令，Ctrl+C 打断当前任务，Ctrl+D 退出。
完整说明见 docs\mole-manual.html（用浏览器打开）。

三、建议另外安装 Git for Windows
agent 执行 ls、grep 这类命令时会用到 Git 自带的 Git Bash：
    winget install -e --id Git.Git

四、配置和数据放在哪里
- 本目录的 .env、models.toml：API key 和供应商配置（只对这个安装生效）
- %USERPROFILE%\.mole-agent\：对话历史、续聊用的上下文、审计日志、上次选的模型等；
  也可以把 .env、models.toml 放在这里，升级 Mole 时不用再改。

五、升级
解压新版本到新目录，把旧目录里的 .env（改过的话还有 models.toml）复制过去，
在新目录再执行一次 install.ps1，然后删掉旧目录。对话历史在 %USERPROFILE%\.mole-agent，不受影响。

六、卸载
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall
然后删掉这个目录。%USERPROFILE%\.mole-agent 不需要的话也可以一起删掉。

七、常见问题
- 运行 mole.exe 时 Windows 提示「已保护你的电脑」：执行一次 install.ps1（会去掉下载标记），
  或者右键 zip →「属性」→ 勾选「解除锁定」后重新解压。
- 提示「在此系统上禁止运行脚本」：按上面的写法用 powershell -ExecutionPolicy Bypass -File 运行 install.ps1。
- mole.exe 被杀毒软件拦截：可以改用同目录的 mole.cmd，用法完全一样。
- 连不上模型：执行 mole --check，它会打出异常链和网络诊断（代理、NO_PROXY、DNS、证书等）。
