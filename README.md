# ProxUI

A modern web-based management interface for Proxmox VE with multi-cluster support and focus on desktop and mobile experience.

## Features

ProxUI isn't a replacement for Proxmox web interface, but a light-weight addition to represent available information in more user friendy way as well as filling some gaps. It has essential functionality to manage single instance(s) or cluster(s).

### Virtual Machine Management
- Represents VM and container status and performance metrics in more readable way
- Creation VMs and containers from templates
- Built-in configuration editor
- QEMU Guest Agent integration for disk usage monitoring
- Consolidated configuration single-page view incluing cloud-init params (like ssh keys)

### Storage Management
- Unified storage view across all cluster nodes
- Group shared media
- Real-time storage usage monitoring

### System Monitoring
- Dashboard with cluster overview and statistics
- Task monitoring
- Network configuration viewing
- Real-time basic resource usage charts

### Multi-Cluster Management
- Connect and manage multiple Proxmox clusters or individual servers from a single interface
- Easy cluster switching with persistent user preferences
- Cluster connection management and status monitoring

### User Experience
- Focus on desktop and mobile devices

## Quick Start

### Using Docker Compose (Recommended)

1. Clone the repository:
```bash
git clone <repository-url>
cd proxui
```

2. Start the application:
```bash
docker-compose up -d
```

3. Access the web interface at `http://localhost:8080`

4. Configure your first Proxmox cluster through the connect page

### Manual Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Access the web interface at `http://localhost:8080`

## Configuration

The application stores configuration in `/app/data/config.toml` (or `./data/config.toml` in development). The configuration file supports multiple clusters:

```toml
[[clusters]]
id = "homelab"
name = "Home Lab"
[[clusters.nodes]]
host = "192.168.1.100:8006"
user = "root@pam"
password = "your-password"
verify_ssl = false
```

Configuration file can be managed manually or by application.

## Custom User creation

If you are willing to use ProxUI with read-only user, create new one by running next command on one of the ProxMox servers:

```sh
# Create new user
pveum user add proxui-readonly@pve --password yourpassword --comment "ProxUI read-only user"

# Assign built-in PVEAuditor role to root path with propagation
pveum acl modify / --users proxui-readonly@pve --roles PVEAuditor --propagate 1
```

## Docker Deployment

The application is designed for Docker deployment with persistent volume mounting:

```yaml
version: '3.8'
services:
  proxui:
    image: git.lagoland.xyz/greenlogles/proxui2:latest
    container_name: proxui
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - CONFIG_FILE_PATH=/app/data/config.toml
    restart: unless-stopped
```

## Requirements

- Python 3.7+
- Flask 2.3.3
- proxmoxer 2.0.1
- Access to Proxmox VE cluster(s)

## Security Notes

- Configuration files contain sensitive credentials
- Use strong passwords and consider API tokens instead of root passwords
- SSL certificate verification can be disabled for self-signed certificates
- Ensure proper network security when exposing the web interface

## Support

If you have any questions, need assistance, please open issue or discussion here on GitHub.

## Author(s)

Alex Shut @greenlogles (https://github.com/greenlogles/ProxUI)
