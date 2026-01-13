# Dockerfile for kernel tool from Maple-Circuit
# Source: https://github.com/MapleCircuit/KernelInfo-Parser
# Date: 2026-01-12

FROM python:3.11-slim

# Argument to specify the version/branch/tag to pull from GitHub
# Defaults to main branch
ARG KERNELINFO_PARSER_VERSION=main

# MySQL connection configuration (for host database)
# Use host.docker.internal to connect to MySQL running on the host
ARG MYSQL_HOST=host.docker.internal
ARG MYSQL_PORT=3306
ARG MYSQL_USER=root
ARG MYSQL_PASSWORD=Passe123  #Should be in an ENV variable
ARG MYSQL_DATABASE=test

# Image metadata
LABEL maintainer="mbeware"
LABEL description="Install kernel tool from Maple-Circuit"
LABEL version="${KERNELINFO_PARSER_VERSION}"

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV MYSQL_HOST=${MYSQL_HOST}
ENV MYSQL_PORT=${MYSQL_PORT}
ENV MYSQL_USER=${MYSQL_USER}
ENV MYSQL_PASSWORD=${MYSQL_PASSWORD}
ENV MYSQL_DATABASE=${MYSQL_DATABASE}

# Install system dependencies
# - git: for git operations on Linux kernel
# - libclang-dev: for Python clang bindings
# - default-libmysqlclient-dev: for MySQL connector
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libclang-dev \
    default-libmysqlclient-dev \
    build-essential \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Create working directory
WORKDIR /app/KernelInfo-Parser

#### Option 1 :  Clone KernelInfo-Parser repository from GitHub with specified version
#RUN git clone --branch ${KERNELINFO_PARSER_VERSION} --depth 1 \
#    https://github.com/MapleCircuit/KernelInfo-Parser.git /app
#
#### Option 2 :  Clone your own version to test your changed. 
# RUN git clone --branch ${KERNELINFO_PARSER_VERSION} --depth 1 \
#    https://github.com/mbeware/KernelInfo-Parser.git /app/KernelInfo-Parser
#
#### Option 3 :  Use local directory that will be mounted as a volume
#### See VOLUME ["/app/KernetInfo-Parser"] a little further

# Create directory for Linux kernel source (to be mounted as volume)
RUN mkdir -p linux

# Install Python dependencies
RUN pip install --no-cache-dir \
    mysql-connector-python \
    libclang

# Volume for Linux kernel source
# Mount from host: -v ~/Downloads/linux-kernel:/app/linux
VOLUME ["/app/KernelInfo-Parser/linux"]
#### Option 3 - mountpoint
VOLUME ["/app/KernelInfo-Parser"]

RUN git config --global --add safe.directory /app/KernelInfo-Parser/linux/.git
# Default entrypoint (we are still in the WORKDIR /app/KernelInfo-Parser)
ENTRYPOINT ["python", "main.py"]

# Usage:
# Build: docker build --build-arg KERNELINFO_PARSER_VERSION=main -t kernelinfo-parser .
# Run:   sudo docker run --add-host=host.docker.internal:host-gateway --tmpfs /dev/shm:rw,exec,size=1g  -v ~/Downloads/linux:/app/KernelInfo-Parser/linux  -v ~/Documents/dev/KernelInfo-Parser:/app/KernelInfo-Parser kernelinfo-parser
