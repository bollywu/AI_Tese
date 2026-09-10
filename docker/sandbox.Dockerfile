# AIcoverage 通用沙箱基础镜像（所有被测项目共享一份，`aicov sandbox` 构建）。
#
# 内容 = 确定性执行面所需的最小集合：
#   - build-essential (gcc/g++/make)   插桩构建（--coverage）
#   - autoconf/automake/libtool/pkg-config  autotools 类项目的 build_cmd 常见依赖
#   - python3 + pytest                  pytest 执行 + 容器内 gcov 采集（aicov CLI 复用）
#   - gcov 随 gcc 附带，与容器内 gcc 天然同源（这正是「采集入容器」要解决的问题）
#
# 被测项目自身的运行时依赖（动态库等）如有缺失，用 [sandbox] extra_args 挂载
# 或在本镜像基础上 FROM 派生，不要改这份文件。
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        autoconf automake libtool pkg-config \
        ca-certificates \
        python3 python3-pip \
    && pip3 install --break-system-packages --no-cache-dir pytest \
    && rm -rf /var/lib/apt/lists/*

# 非 root 运行（--user $(id -u):$(id -g) 时以数字 uid 执行，无需 passwd 条目）
WORKDIR /workspace
