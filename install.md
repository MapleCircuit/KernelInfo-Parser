1. Dockerfile 

Linux kernel volume mount at /app/linux (mount from ~/Downloads/linux-kernel)

MySQL connection configured to use host.docker.internal to connect to host database

Environment variables for MySQL configuration

2. setup_mysql.sh - script that:

Detects OS (Debian/Ubuntu, RedHat/Fedora, Windows/WSL)

Installs MySQL automatically on Linux

Configures the database (test) with user root / password Passe123

Enables remote connections for Docker access

All log messages have IDs (MYSQL001-MYSQL029)

Usage:

# 0. Pre-Installation

## 0.1 Install docker or docker-desktop or any other container technology compatible with docker containers.
```bash
sudo apt install docker.io # for official docker on ubuntu
```
## 0.2 Download kernel sources
```bash
cd ~/Download
git clone --shallow-since="2025-01-01" https://github.com/torvalds/linux.git # remove --shallow... parameter for everything (10-15Gbytes).
mv linux linux-kernel
```
# 1. Setup MySQL on host
```bash
chmod +x setup_mysql.sh
./setup_mysql.sh
```
# 2. Build Docker image 

```bash
# if we added current user to docker group, there would be no need for sudo
sudo docker build -t kernelinfo-parser .
```

# 3. Run with Linux kernel mounted
```bash
# if we added current user to docker group, there would be no need for sudo. 6g of sharedmemory/ramdisk. increase if needed.
sudo docker run --add-host=host.docker.internal:host-gateway --tmpfs /dev/shm:rw,exec,size=6g  -v ~/Downloads/linux:/app/KernelInfo-Parser/linux  -v ~/Documents/dev/KernelInfo-Parser:/app/KernelInfo-Parser kernelinfo-parser
```
