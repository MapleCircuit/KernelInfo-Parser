#!/bin/bash
# MySQL installation and configuration script for kernel-tool from Maple-Circuit
# This script downloads, installs and configures MySQL on Linux or Windows (WSL)
# Date: 2026-01-12

set -e

# Colors for messages --- wink, wink
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Database configuration for kernel tool from Maple-Cricuit
DB_NAME="test"
DB_USER="root"
DB_PASSWORD="Passe123"

log_info() {
    local log_id=$1
    local message=$2
    echo -e "${GREEN}[INFO]${NC} [${log_id}] ${message}"
}

log_warn() {
    local log_id=$1
    local message=$2
    echo -e "${YELLOW}[WARN]${NC} [${log_id}] ${message}"
}

log_error() {
    local log_id=$1
    local message=$2
    echo -e "${RED}[ERROR]${NC} [${log_id}] ${message}"
}

### begin stolen code from a previous workplace
# Detect operating system 
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/debian_version ]; then
            echo "debian"
        elif [ -f /etc/redhat-release ]; then
            echo "redhat"
        else
            echo "linux"
        fi
    elif [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "cygwin" ]] || [[ -n "$WSL_DISTRO_NAME" ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

# Install MySQL on Debian/Ubuntu
install_mysql_debian() {
    log_info "MYSQL001" "Installing MySQL on Debian/Ubuntu..."
    
    sudo apt-get update
    sudo apt-get install -y mysql-server mysql-client
    
    # Start MySQL service
    sudo systemctl start mysql
    sudo systemctl enable mysql
    
    log_info "MYSQL002" "MySQL installed successfully on Debian/Ubuntu"
}

# Install MySQL on RedHat/CentOS/Fedora
install_mysql_redhat() {
    log_info "MYSQL003" "Installing MySQL on RedHat/CentOS/Fedora..."
    
    sudo dnf install -y mysql-server mysql
    
    # Start MySQL service
    sudo systemctl start mysqld
    sudo systemctl enable mysqld
    
    log_info "MYSQL004" "MySQL installed successfully on RedHat/CentOS/Fedora"
}

# Instructions for Windows
install_mysql_windows() {
    log_info "MYSQL005" "Windows/WSL detected..."
    log_warn "MYSQL006" "For Windows, please download MySQL from:"
    log_warn "MYSQL007" "https://dev.mysql.com/downloads/installer/"
    log_warn "MYSQL008" "Or use winget: winget install Oracle.MySQL"
    log_warn "MYSQL009" "After installation, run this script again to configure the database."
    
    # Check if MySQL is already installed
    if command -v mysql &> /dev/null; then
        log_info "MYSQL010" "MySQL appears to be installed. Proceeding with configuration..."
        return 0
    else
        log_error "MYSQL011" "MySQL is not installed or not in PATH."
        exit 1
    fi
}

# Configure MySQL 
configure_mysql() {
    log_info "MYSQL012" "Configuring MySQL for kernel tool from Maple-Cricuit..."
    
    # Wait for MySQL to be ready
    local max_attempts=30
    local attempt=0
    
    while ! mysqladmin ping -h localhost --silent 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            log_error "MYSQL013" "MySQL is not accessible after $max_attempts attempts"
            exit 1
        fi
        log_info "MYSQL014" "Waiting for MySQL... ($attempt/$max_attempts)"
        sleep 2
    done
    
    log_info "MYSQL015" "MySQL is ready. Configuring database..."
    
    # Try connecting without password (fresh installation)
    if sudo mysql -u root -e "SELECT 1" 2>/dev/null; then
        log_info "MYSQL016" "Passwordless connection successful. Setting root password..."
        
        sudo mysql -u root <<EOF
ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '${DB_PASSWORD}';
FLUSH PRIVILEGES;
CREATE DATABASE IF NOT EXISTS ${DB_NAME};
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO 'root'@'localhost';
CREATE USER 'root'@'%' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';
GRANT SUPER ON *.* TO 'root'@'%';
FLUSH PRIVILEGES;
EOF
    else
        # Try with configured password
        log_info "MYSQL017" "Attempting connection with configured password..."
        
        mysql -u root -p"${DB_PASSWORD}" <<EOF
ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '${DB_PASSWORD}';
FLUSH PRIVILEGES;
CREATE DATABASE IF NOT EXISTS ${DB_NAME};
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO 'root'@'localhost';
CREATE USER 'root'@'%' IDENTIFIED BY '${DB_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';
GRANT SUPER ON *.* TO 'root'@'%';
FLUSH PRIVILEGES;
EOF
    fi
    
    log_info "MYSQL018" "Database '${DB_NAME}' created and configured successfully"
}

### end stolen code from a previous workplace

# Configure MySQL to accept remote connections (for Docker)
configure_mysql_remote_access() {
    log_info "MYSQL019" "Configuring remote access for MySQL..."
    
    # Find MySQL configuration file
    local mysql_conf=""
    
    if [ -f /etc/mysql/mysql.conf.d/mysqld.cnf ]; then
        mysql_conf="/etc/mysql/mysql.conf.d/mysqld.cnf"
    elif [ -f /etc/mysql/my.cnf ]; then
        mysql_conf="/etc/mysql/my.cnf"
    elif [ -f /etc/my.cnf ]; then
        mysql_conf="/etc/my.cnf"
    fi
    
    if [ -n "$mysql_conf" ]; then
        log_info "MYSQL020" "Modifying $mysql_conf to allow external connections..."
        
        # Backup original file
        sudo cp "$mysql_conf" "${mysql_conf}.backup"
        
        # Modify bind-address to accept all connections
        if grep -q "bind-address" "$mysql_conf"; then
            sudo sed -i 's/bind-address\s*=.*/bind-address = 0.0.0.0/' "$mysql_conf"
        else
            echo "bind-address = 0.0.0.0" | sudo tee -a "$mysql_conf"
        fi
        
        # Restart MySQL
        sudo systemctl restart mysql 2>/dev/null || sudo systemctl restart mysqld 2>/dev/null
        
        log_info "MYSQL021" "MySQL configured to accept external connections"
    else
        log_warn "MYSQL022" "MySQL configuration file not found. Manual configuration required."
    fi
}

# Display connection information
print_connection_info() {
    echo ""
    log_info "MYSQL023" "============================================"
    log_info "MYSQL024" "MySQL configuration for kernel tool from Maple-Cricuit complete"
    log_info "MYSQL025" "============================================"
    echo ""
    echo "Connection information:"
    echo "  Host:     localhost (or host.docker.internal from Docker)"
    echo "  Port:     3306"
    echo "  Database: ${DB_NAME}"
    echo "  User:     ${DB_USER}"
    echo "  Password: ${DB_PASSWORD}"
    echo ""
    echo "To run KernelInfo-Parser with Docker:"
    echo "  sudo docker run --add-host=host.docker.internal:host-gateway \\"
    echo "                  --tmpfs /dev/shm:rw,exec,size=6g \\"
    echo "                  -v ~/Downloads/linux:/app/KernelInfo-Parser/linux \\"
    echo "                  -v ~/Documents/dev/KernelInfo-Parser:/app/KernelInfo-Parser \\"
    echo "                  kernelinfo-parser"
    echo ""
}

# Main function
main() {
    log_info "MYSQL026" "Starting MySQL installation and configuration for kernel tool from Maple-Cricuit"
    
    OS=$(detect_os)
    log_info "MYSQL027" "Detected operating system: $OS"
    
    case $OS in
        debian)
            install_mysql_debian
            configure_mysql
            configure_mysql_remote_access
            ;;
        redhat)
            install_mysql_redhat
            configure_mysql
            configure_mysql_remote_access
            ;;
        windows)
            install_mysql_windows
            configure_mysql
            ;;
        *)
            log_error "MYSQL028" "Unsupported operating system: $OS"
            log_warn "MYSQL029" "Please install MySQL manually and run this script again."
            exit 1
            ;;
    esac
    
    print_connection_info
}

# Run script
main "$@"
