# 沙箱镜像 —— 用户代码真正运行的地方
#
# 由 OpenSandbox server 在建机时使用（本地 Docker 运行时与生产 K8s 运行时都用它），
# 所以同一份定义装出来的运行时一致，不会出现「本地跑得好、上生产就缺依赖」。
#
# ## 这里装的东西是为了让「运行时零安装」
#
# 模型在沙箱里最常走的路是 dataframe 分析 + 出图 + 拉 git 仓库 + 在代码里搜字符串。
# 这些不在镜像里，第一条命令就得 `pip install` / `apt-get install`：每次都要网络
# （`SANDBOX_EGRESS=proxy` 下根本没有网络）、每次几十秒、每次都解析出一棵新依赖树。
# 装进模板后冷启动从「分钟级」变成「容器起来就能跑」。
#
# 反过来说：**不要在这里加编译器与构建工具链**。进来了就会有人开始在运行时
# `pip install --no-binary` 编译包，而那既没有网络也没人保证成功。
#
# ## 为什么 uid 必须是 1000，以及为什么由镜像自己声明
#
# 运行用户完全由镜像的 `USER` 决定：OpenSandbox 的 execd 不传 `--user`，而实测给命令
# 传 `uid/gid` 会让执行结果整个丢失（`exit_code=None`、stdout 为空，见
# `app/services/sandbox/opensandbox.py` 的模块注释）。所以这里必须自己建 uid 1000 的
# 用户并声明它——不是 1000 的话，`/home/sandbox/workspace` 里的文件归属会全错，
# 模型写进去的文件下一个命令就改不动。
#
# ## 为什么非 root
#
# 容器能力在服务端配置里已经裁剪（`docker/opensandbox.toml` 的 drop_capabilities +
# no_new_privileges）。以非 root 运行让「沙箱内的越权」不至于顺带变成「容器内任意文件」。
# 注意这**不是安全边界**：Docker 运行时共享宿主内核，只用于本地开发与 CI；
# 生产的隔离来自 Firecracker 的独立内核。
#
# 构建（版本号要与 SANDBOX_TEMPLATE_VERSION 一致）：
#   docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 .
# 版本号与 `SANDBOX_TEMPLATE_VERSION` 对应：租约上记的版本必须能对上某个真实镜像。

FROM python:3.12-slim

# - PYTHONUNBUFFERED：沙箱里的输出要按序回到 exec 的 stdout，缓冲会把 stderr 和
#   stdout 的先后关系打散，模型看到的「报错顺序」就错了；
# - PIP_NO_CACHE_DIR：镜像里不需要 pip 的 wheel 缓存，它只会让镜像大几百 MB；
# - MPLCONFIGDIR：matplotlib 会往 `$HOME/.config/matplotlib` 写字体缓存，指到 /tmp
#   能让容器重建时不留隐藏状态（字体缓存是可重建的，不该进工作目录）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/matplotlib

# `--no-install-recommends`：Debian 的 Recommends 会顺带拉进一大堆可选包，它们既让
# 镜像变大，也都成了要跟着打补丁的东西。需要什么就显式写出来。
#
# - coreutils：`timeout(1)` 与 `sleep infinity` 都来自它——provider 的 exec 超时包一层
#   `timeout`，容器主进程是 `sleep infinity`，两个都缺了容器会立刻退出；
# - ca-certificates + curl：模型最常见的动作就是 curl 一个公开数据源；
# - git / ripgrep：拉仓库、在代码里找字符串；
# - procps：`ps`/`top`，模型排障（「我的进程还在吗」）时会用，缺了只能看到
#   `command not found` 然后开始乱猜。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        curl \
        git \
        procps \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# 依赖装进系统 site-packages（非 root 用户也能用，不需要 venv）。版本**不钉死**：
# 「这台机器跑的是哪一版运行时」由租约上的 `template_version` 承担，钉死 pip 版本
# 只会让每次安全更新都必须手工改 Dockerfile 再重建；重建即新版本号。
RUN pip install --no-cache-dir \
        matplotlib \
        pandas \
        pillow \
        requests

# uid/gid 显式给 1000：`useradd` 的自动分配在别的发行版/别的 base 上可能变成 1001，
# 而 provider 硬编码了 1000。`--create-home` 是为了让 `$HOME` 可写（matplotlib、git
# 都要写点东西），否则会看到一类「只读文件系统」的误导性报错。
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/sandbox \
        --shell /bin/bash sandbox \
    && mkdir -p /home/sandbox/workspace \
    && chown -R 1000:1000 /home/sandbox

WORKDIR /home/sandbox/workspace

USER sandbox

# 主进程的默认值。provider 会显式传 `sleep infinity`（容器的生命周期由租约决定，
# 不能随最后一条命令结束）；这条 CMD 只是让 `docker run <image>` 也能起一台可 exec
# 的容器来排障——没有它，`docker run` 会因为「没有命令」直接失败。
CMD ["sleep", "infinity"]
