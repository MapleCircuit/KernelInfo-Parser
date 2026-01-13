#!/bin/bash
# MySQL installation and configuration script for kernel-tool from Maple-Circuit
# This script downloads, installs and configures MySQL on Linux with apt
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
EOF
     else
        # Try with configured password
        log_info "MYSQL017" "Attempting connection with configured password..."
        
        mysql -u root -p"${DB_PASSWORD}" <<EOF
ALTER USER 'root'@'localhost' IDENTIFIED WITH mysql_native_password BY '${DB_PASSWORD}';
FLUSH PRIVILEGES;
EOF
    fi

    log_info "MYSQL016-17b" "creating remote login"
    mysql -u root -p"${DB_PASSWORD}" <<EOF
CREATE USER 'root'@'%' IDENTIFIED BY '${DB_PASSWORD}';
GRANT SUPER ON *.* TO 'root'@'%';
FLUSH PRIVILEGES;
EOF

    log_info "MYSQL016-17c" "creating database"
    mysql -u root -p"${DB_PASSWORD}" <<EOF
CREATE DATABASE IF NOT EXISTS ${DB_NAME};
GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO 'root'@'localhost';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';
FLUSH PRIVILEGES;
EOF

    log_info "MYSQL016-17c" "displaying databases and users"
    mysql -u root -p"${DB_PASSWORD}" <<EOF
SHOW DATABASES;
USE mysql;
SELECT User, Host FROM mysql.user;
EOF

    log_info "MYSQL018" "Database '${DB_NAME}' created and configured successfully"
}

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

# Main function
main() {
    log_info "MYSQL026" "Starting MySQL installation and configuration for kernel tool from Maple-Cricuit"
    
    install_mysql_debian
    configure_mysql
    configure_mysql_remote_access
}

# Run script
main "$@"
