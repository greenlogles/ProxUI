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

## Screenshots

### Desktop
<div style="display: flex; gap: 10px;">
  <img src="misc/screenshots/main.png" alt="Main Dashboard" width="400">
  <img src="misc/screenshots/vms.png" alt="Virtual Machines List" width="400">
  <img src="misc/screenshots/vmdetails.png" alt="VM Details" width="400">
  <img src="misc/screenshots/storage.png" alt="Storage Overview" width="400">
  <img src="misc/screenshots/isos_templates.png" alt="ISOs and Templates" width="400">
  <img src="misc/screenshots/clusterinfo.png" alt="Cluster view" width="400">
  <img src="misc/screenshots/clustermanage.png" alt="Manage hosts and clusters" width="400">
  <img src="misc/screenshots/tasks.png" alt="Tasks" width="400">
</div>

### Mobile
<div style="display: flex; gap: 10px;">
  <img src="misc/screenshots/mobile_1.png" alt="Mobile View 1" width="200">
  <img src="misc/screenshots/mobile_2.png" alt="Mobile View 2" width="200">
  <img src="misc/screenshots/mobile_3.png" alt="Mobile View 3" width="200">
  <img src="misc/screenshots/mobile_4.png" alt="Mobile View 4" width="200">
</div>

## Quick Start

### Using Docker Compose (Recommended)

1. Clone the repository:
```bash
git clone https://github.com/greenlogles/proxui
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

The application is designed for Docker deployment with persistent volume mounting. Docker images are automatically built and published via CI/CD pipeline.

### Available Image Tags

- **`ghcr.io/greenlogles/proxui:latest`** - Latest stable release (recommended for production)
- **`ghcr.io/greenlogles/proxui:testing`** - Latest development build from main branch
- **`ghcr.io/greenlogles/proxui:v1.2.3`** - Specific version tags for releases

### Docker Compose (Recommended)

```yaml
version: '3.8'
services:
  proxui:
    image: ghcr.io/greenlogles/proxui:latest  # or :testing for latest development
    container_name: proxui
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
    environment:
      - CONFIG_FILE_PATH=/app/data/config.toml
    restart: unless-stopped
```

### Docker Run

```bash
# Latest stable release
docker run -d \
  --name proxui \
  -p 8080:8080 \
  -v ./data:/app/data \
  -e CONFIG_FILE_PATH=/app/data/config.toml \
  ghcr.io/greenlogles/proxui:latest

# Latest development build
docker run -d \
  --name proxui \
  -p 8080:8080 \
  -v ./data:/app/data \
  -e CONFIG_FILE_PATH=/app/data/config.toml \
  ghcr.io/greenlogles/proxui:testing
```

### CI/CD and Automated Builds

ProxUI uses GitHub Actions for automated testing and Docker image building with GitHub Container Registry:

- **Testing Images**: Built automatically on every push to `main` branch (`:testing` tag)
- **Release Images**: Built automatically when GitHub releases are created (`:latest` + version tags)
- **Multi-platform**: Images support both `linux/amd64` and `linux/arm64`
- **No Setup Required**: Uses built-in GitHub authentication and permissions

Images are published to GitHub Container Registry at `ghcr.io/greenlogles/proxui`.

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
