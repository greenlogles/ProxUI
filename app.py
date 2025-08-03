from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from proxmoxer import ProxmoxAPI
import toml
import requests
from functools import wraps
from datetime import datetime
import urllib3
from collections import defaultdict

import pprint

# Disable SSL warnings if needed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Load configuration
try:
    with open('config.toml', 'r') as f:
        config = toml.load(f)
except Exception as e:
    print(f"Error loading config.toml: {e}")
    config = {'cluster': {'name': 'Proxmox Cluster'}, 'nodes': []}

# Store Proxmox connections
proxmox_nodes = {}
cluster_nodes = []  # Store all nodes from all connections

def init_proxmox_connections():
    """Initialize connections to all Proxmox nodes and discover cluster members"""
    global cluster_nodes
    discovered_nodes = set()
    
    # First, connect to configured nodes
    for node_config in config['nodes']:
        try:
            proxmox = ProxmoxAPI(
                node_config['host'],
                user=node_config['user'],
                password=node_config['password'],
                verify_ssl=node_config.get('verify_ssl', True)
            )
            
            # Test connection
            version = proxmox.version.get()
            print(f"Connected to {node_config['host']} - PVE {version['version']}")
            
            # Get all nodes in the cluster through this connection
            try:
                nodes = proxmox.nodes.get()
                for node in nodes:
                    node_name = node['node']
                    if node_name not in discovered_nodes:
                        discovered_nodes.add(node_name)
                        # Store the connection that can access this node
                        if node_name not in proxmox_nodes:
                            proxmox_nodes[node_name] = proxmox
                        cluster_nodes.append({
                            'name': node_name,
                            'status': node['status'],
                            'connection': proxmox
                        })
            except Exception as e:
                print(f"Error getting cluster nodes from {node_config['host']}: {e}")
                # If cluster endpoint fails, at least add this node
                proxmox_nodes[node_config['host']] = proxmox
                
        except Exception as e:
            print(f"Failed to connect to {node_config['host']}: {e}")
    
    print(f"Discovered {len(cluster_nodes)} cluster nodes: {[n['name'] for n in cluster_nodes]}")
    print(f"Active connections to {len(proxmox_nodes)} nodes")

def get_proxmox_for_node(node_name):
    """Get the appropriate Proxmox connection for a specific node"""
    # First check if we have a direct connection to this node
    if node_name in proxmox_nodes:
        return proxmox_nodes[node_name]
    
    # Otherwise, find a connection that can access this node
    for node_info in cluster_nodes:
        if node_info['name'] == node_name:
            return node_info['connection']
    
    # Fallback to any available connection (they should all work in a cluster)
    if proxmox_nodes:
        return next(iter(proxmox_nodes.values()))
    
    return None

def get_all_vms_and_containers():
    """Get all VMs and containers from all nodes using cluster resources endpoint"""
    all_resources = []
    processed_vmids = set()  # Track processed VMs to avoid duplicates
    
    # Use the first available connection to get cluster-wide resources
    if proxmox_nodes:
        try:
            # Get any working connection
            proxmox = next(iter(proxmox_nodes.values()))
            
            # Get all VMs and containers from cluster endpoint
            resources = proxmox.cluster.resources.get(type='vm')
            
            for resource in resources:
                # Skip if we've already processed this VM
                vm_key = f"{resource['node']}-{resource['vmid']}"
                if vm_key in processed_vmids:
                    continue
                processed_vmids.add(vm_key)
                
                # Calculate resource usage percentages
                if resource.get('maxcpu'):
                    resource['cpu_percent'] = (resource.get('cpu', 0) * 100)
                else:
                    resource['cpu_percent'] = 0
                    
                if resource.get('maxmem') and resource.get('maxmem') > 0:
                    resource['mem_percent'] = (resource.get('mem', 0) / resource.get('maxmem', 1)) * 100
                else:
                    resource['mem_percent'] = 0
                    
                if resource.get('maxdisk') and resource.get('maxdisk') > 0:
                    resource['disk_percent'] = (resource.get('disk', 0) / resource.get('maxdisk', 1)) * 100
                else:
                    resource['disk_percent'] = 0
                
                all_resources.append(resource)
                
        except Exception as e:
            print(f"Error getting cluster resources: {e}")
            # Fallback to node-by-node retrieval
            return get_all_vms_and_containers_fallback()
    
    return all_resources

def get_all_vms_and_containers_fallback():
    """Fallback method to get VMs/containers node by node"""
    all_resources = []
    processed_vmids = set()
    
    for node_info in cluster_nodes:
        try:
            node_name = node_info['name']
            proxmox = node_info['connection']
            
            # Get VMs
            vms = proxmox.nodes(node_name).qemu.get()
            for vm in vms:
                vm_key = f"{node_name}-{vm['vmid']}"
                if vm_key not in processed_vmids:
                    processed_vmids.add(vm_key)
                    vm['node'] = node_name
                    vm['type'] = 'qemu'
                    # Calculate percentages
                    vm['cpu_percent'] = vm.get('cpu', 0) * 100 if vm.get('cpu') else 0
                    vm['mem_percent'] = (vm.get('mem', 0) / vm.get('maxmem', 1)) * 100 if vm.get('maxmem') else 0
                    vm['disk_percent'] = (vm.get('disk', 0) / vm.get('maxdisk', 1)) * 100 if vm.get('maxdisk') else 0
                    all_resources.append(vm)
            
            # Get containers
            containers = proxmox.nodes(node_name).lxc.get()
            for container in containers:
                ct_key = f"{node_name}-{container['vmid']}"
                if ct_key not in processed_vmids:
                    processed_vmids.add(ct_key)
                    container['node'] = node_name
                    container['type'] = 'lxc'
                    # Calculate percentages
                    container['cpu_percent'] = container.get('cpu', 0) * 100 if container.get('cpu') else 0
                    container['mem_percent'] = (container.get('mem', 0) / container.get('maxmem', 1)) * 100 if container.get('maxmem') else 0
                    container['disk_percent'] = (container.get('disk', 0) / container.get('maxdisk', 1)) * 100 if container.get('maxdisk') else 0
                    all_resources.append(container)
                    
        except Exception as e:
            print(f"Error getting resources from node {node_info['name']}: {e}")
    
    return all_resources

def get_qemu_guest_disk_info(proxmox, node, vmid):
    """Get disk usage information from QEMU guest agent"""
    try:
        # Check if guest agent is available by trying to get filesystem info
        fsinfo = proxmox.nodes(node).qemu(vmid).agent.get('get-fsinfo')
        disk_info = []
        if isinstance(fsinfo.get('result'), list):
            for fs in fsinfo.get('result'):
                # Extract relevant filesystem information
                disk_info.append({
                    'name': fs.get('name', 'Unknown'),
                    'mountpoint': fs.get('mountpoint', '/'),
                    'type': fs.get('type', 'unknown'),
                    'used_bytes': fs.get('used-bytes', 0),
                    'total_bytes': fs.get('total-bytes', 0),
                    'disk_name': fs.get('disk', [{}])[0].get('serial', 'Unknown') if fs.get('disk') else 'Unknown'
                })
        
        # Calculate usage percentages
        for disk in disk_info:
            if disk['total_bytes'] > 0:
                disk['used_percent'] = (disk['used_bytes'] / disk['total_bytes']) * 100
                disk['used_gb'] = disk['used_bytes'] / (1024**3)
                disk['total_gb'] = disk['total_bytes'] / (1024**3)
                disk['free_gb'] = (disk['total_bytes'] - disk['used_bytes']) / (1024**3)
            else:
                disk['used_percent'] = 0
                disk['used_gb'] = 0
                disk['total_gb'] = 0
                disk['free_gb'] = 0
        
        return disk_info
        
    except Exception as e:
        print(f"Error getting guest agent disk info: {e}")
        return None

def parse_vm_configuration(config, vm_type='qemu'):
    """Parse VM/LXC configuration into structured groups"""
    parsed_config = {
        'cpu': {},
        'memory': {},
        'network': [],
        'storage': [],
        'devices': [],
        'cloud_init': {},
        'general': {},
        'other': {}
    }
    
    for key, value in config.items():
        # CPU Configuration
        if key in ['cores', 'sockets', 'vcpus', 'cpu', 'cpulimit', 'cpuunits']:
            parsed_config['cpu'][key] = value
        
        # Memory Configuration  
        elif key in ['memory', 'balloon', 'shares']:
            parsed_config['memory'][key] = value
            
        # Network Configuration
        elif key.startswith('net'):
            net_info = {'interface': key, 'config': value}
            if '=' in str(value):
                # Parse network config like "virtio,bridge=vmbr0,firewall=1"
                parts = str(value).split(',')
                net_info['model'] = parts[0].split('=')[0] if parts else 'unknown'
                net_info['mac'] = parts[0].split('=')[1] if parts else 'unknown'
                for part in parts[1:]:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        net_info[k] = v
            parsed_config['network'].append(net_info)
            
        # Storage Configuration
        elif key.startswith(('scsi', 'ide', 'sata', 'virtio', 'rootfs', 'mp')):
            storage_info = {'device': key, 'config': value}
            if '=' in str(value) or ':' in str(value):
                # Parse storage config like "local-lvm:vm-100-disk-0,size=32G"
                storage_info['details'] = str(value)
            parsed_config['storage'].append(storage_info)
            
        # Hardware Devices
        elif key.startswith(('hostpci', 'usb', 'serial', 'audio')):
            parsed_config['devices'].append({'device': key, 'config': value})
            
        # Cloud-init Configuration
        elif key in ['ciuser', 'cipassword', 'sshkeys', 'ipconfig0', 'ipconfig1', 'ipconfig2', 'nameserver', 'searchdomain', 'cicustom']:
            if key == 'sshkeys':
                # Parse SSH keys to extract meaningful information
                parsed_config['cloud_init'][key] = {
                    'raw': value,
                    'parsed': parse_ssh_keys(value)
                }
            else:
                parsed_config['cloud_init'][key] = value
            
        # General VM Settings
        elif key in ['name', 'ostype', 'boot', 'bootdisk', 'onboot', 'startup', 'protection', 'template', 'tags']:
            parsed_config['general'][key] = value
            
        # Everything else
        else:
            parsed_config['other'][key] = value
    
    # Add default CPU values if CPU section is empty (Proxmox defaults)
    if not parsed_config['cpu']:
        parsed_config['cpu'] = {
            'sockets': 1,
            'cores': 1
        }
    else:
        # Ensure default values exist if not specified
        if 'sockets' not in parsed_config['cpu']:
            parsed_config['cpu']['sockets'] = 1
        if 'cores' not in parsed_config['cpu']:
            parsed_config['cpu']['cores'] = 1
    
    return parsed_config

def parse_ssh_keys(ssh_keys_string):
    """Parse SSH keys from URL-encoded string and extract key info"""
    if not ssh_keys_string:
        return []
    
    import urllib.parse
    
    # URL decode the string
    decoded_keys = urllib.parse.unquote(ssh_keys_string)
    
    # Split by newlines to get individual keys
    key_lines = [line.strip() for line in decoded_keys.split('\n') if line.strip()]
    
    parsed_keys = []
    for key_line in key_lines:
        if not key_line:
            continue
            
        # SSH key format: <type> <key-data> <comment>
        parts = key_line.split(' ', 2)
        if len(parts) >= 2:
            key_type = parts[0]  # ssh-rsa, ssh-ed25519, etc.
            key_data = parts[1]  # The actual key data
            comment = parts[2] if len(parts) > 2 else 'no comment'
            
            # Truncate key data for display
            key_preview = key_data[:10] + '...' + key_data[-10:] if len(key_data) > 20 else key_data
            
            parsed_keys.append({
                'type': key_type,
                'preview': key_preview,
                'comment': comment,
                'full_key': key_line
            })
        else:
            # Fallback for malformed keys
            parsed_keys.append({
                'type': 'unknown',
                'preview': key_line[:30] + '...' if len(key_line) > 30 else key_line,
                'comment': 'malformed key',
                'full_key': key_line
            })
    
    return parsed_keys

# ROUTES - Make sure all routes are defined

@app.route('/')
def index():
    """Dashboard with overview"""
    resources = get_all_vms_and_containers()
    
    # Calculate statistics
    total_vms = len([r for r in resources if r['type'] == 'qemu'])
    total_containers = len([r for r in resources if r['type'] == 'lxc'])
    running = len([r for r in resources if r.get('status') == 'running'])
    stopped = len([r for r in resources if r.get('status') == 'stopped'])
    
    # Find top resource consumers
    top_cpu = sorted(resources, key=lambda x: x.get('cpu', 0), reverse=True)[:8]
    top_memory = sorted(resources, key=lambda x: x.get('mem', 0), reverse=True)[:8]
    
    return render_template('index.html',
                         total_vms=total_vms,
                         total_containers=total_containers,
                         running=running,
                         stopped=stopped,
                         top_cpu=top_cpu,
                         top_memory=top_memory)

@app.route('/vms')
def vms():
    """List all VMs and containers"""
    resources = get_all_vms_and_containers()
    
    # Sort by CPU usage by default
    sort_by = request.args.get('sort', 'cpu')
    reverse = request.args.get('order', 'desc') == 'desc'
    
    if sort_by == 'cpu':
        resources.sort(key=lambda x: x.get('cpu', 0), reverse=reverse)
    elif sort_by == 'memory':
        resources.sort(key=lambda x: x.get('mem', 0), reverse=reverse)
    elif sort_by == 'disk':
        resources.sort(key=lambda x: x.get('disk', 0), reverse=reverse)
    elif sort_by == 'name':
        resources.sort(key=lambda x: x.get('name', ''), reverse=reverse)
    
    return render_template('vms.html', resources=resources)

@app.route('/vm/<node>/<vmid>')
def vm_detail(node, vmid):
    """Show detailed VM information"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        flash('Node connection not found', 'error')
        return redirect(url_for('vms'))
    
    try:
        # Get VM config
        vm_type = 'qemu'  # Default to qemu
        try:
            config = proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = 'lxc'
            config = proxmox.nodes(node).lxc(vmid).config.get()
        
        # Get current status
        if vm_type == 'qemu':
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
        else:
            status = proxmox.nodes(node).lxc(vmid).status.current.get()
        
        # Get available nodes for migration (all cluster nodes except current)
        available_nodes = [n['name'] for n in cluster_nodes if n['name'] != node and n.get('status') == 'online']
        
        # Get guest agent disk information for QEMU VMs
        guest_disk_info = None
        if vm_type == 'qemu' and status.get('status') == 'running':
            guest_disk_info = get_qemu_guest_disk_info(proxmox, node, vmid)
        
        # Check for active migration tasks
        migration_info = None
        try:
            all_tasks = proxmox.nodes(node).tasks.get()
            for task in all_tasks:
                task_id = task.get('id', '')
                task_type = task.get('type', '')
                task_status = task.get('status', '').lower()
                
                # Check if this is an active migration task for this VM/container
                if (task_type in ['qmigrate', 'vzmigrate'] and
                    vmid in str(task_id) and 
                    'running' in task_status):
                    
                    migration_info = {
                        'status': task.get('status'),
                        'target': task.get('target'),
                        'start_time': datetime.fromtimestamp(task.get('starttime')).strftime('%Y-%m-%d %H:%M:%S') if task.get('starttime') else 'Unknown',
                        'task_id': task.get('upid')
                    }
                    break
        except Exception as e:
            print(f"Error checking migration status: {e}")
        
        # Parse configuration into structured groups
        parsed_config = parse_vm_configuration(config, vm_type)
        
        return render_template('vm_detail.html',
                             vm_type=vm_type,
                             vmid=vmid,
                             node=node,
                             config=config,
                             parsed_config=parsed_config,
                             status=status,
                             available_nodes=available_nodes,
                             migration_info=migration_info,
                             guest_disk_info=guest_disk_info)
    except Exception as e:
        flash(f'Error getting VM details: {e}', 'error')
        return redirect(url_for('vms'))

@app.route('/vm/<node>/<vmid>/<action>', methods=['POST'])
def vm_action(node, vmid, action):
    """Perform action on VM"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node connection not found'}), 404
    
    try:
        # Determine VM type
        vm_type = 'qemu'
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = 'lxc'
        
        # Get VM object
        if vm_type == 'qemu':
            vm = proxmox.nodes(node).qemu(vmid)
        else:
            vm = proxmox.nodes(node).lxc(vmid)
        
        # Perform action
        if action == 'start':
            vm.status.start.post()
            flash(f'VM {vmid} started successfully', 'success')
        elif action == 'stop':
            vm.status.stop.post()
            flash(f'VM {vmid} stopped successfully', 'success')
        elif action == 'shutdown':
            vm.status.shutdown.post()
            flash(f'VM {vmid} shutdown initiated', 'success')
        elif action == 'reset':
            vm.status.reset.post()
            flash(f'VM {vmid} reset successfully', 'success')
        elif action == 'migrate':
            target_node = request.form.get('target_node')
            target_storage = request.form.get('target_storage')
            
            # Check if migration is already in progress
            try:
                all_tasks = proxmox.nodes(node).tasks.get()
                for task in all_tasks:
                    task_id = task.get('id', '')
                    task_type = task.get('type', '')
                    task_status = task.get('status', '').lower()
                    
                    if (task_type in ['qmigrate', 'vzmigrate'] and
                        vmid in str(task_id) and
                        'running' in task_status):
                        flash('Migration already in progress. Please wait for current migration to complete.', 'warning')
                        return redirect(url_for('vm_detail', node=node, vmid=vmid))
            except Exception as e:
                print(f"Error checking migration status: {e}")
            
            if target_node:
                vm_type_name = 'VM' if vm_type == 'qemu' else 'Container'
                
                if target_storage:
                    # Two-step process: migrate VM/container, then move storage
                    try:
                        # Step 1: Migrate the VM/container with local disks
                        migrate_params = {
                            'target': target_node,
                            'online': 1
                        }
                        
                        # Always migrate with local disks when storage is specified
                        if vm_type == 'qemu':
                            migrate_params['with-local-disks'] = 1
                        else:
                            migrate_params['with-local-disks'] = 1
                        
                        vm.migrate.post(**migrate_params)
                        
                        flash(f'{vm_type_name} {vmid} migration to {target_node} started (storage will remain on current storage)', 'success')
                        
                    except Exception as migrate_error:
                        # If migration with storage fails, try without storage migration
                        try:
                            simple_params = {
                                'target': target_node,
                                'online': 1
                            }
                            vm.migrate.post(**simple_params)
                            flash(f'{vm_type_name} {vmid} migration to {target_node} started (storage migration not supported)', 'warning')
                        except Exception as simple_error:
                            raise migrate_error
                else:
                    # Simple migration without storage
                    migrate_params = {
                        'target': target_node,
                        'online': 1
                    }
                    vm.migrate.post(**migrate_params)
                    flash(f'{vm_type_name} {vmid} migration to {target_node} started', 'success')
            else:
                flash('Target node not specified', 'error')
        else:
            flash(f'Unknown action: {action}', 'error')
            
    except Exception as e:
        flash(f'Error performing action: {e}', 'error')
    
    return redirect(url_for('vm_detail', node=node, vmid=vmid))

@app.route('/tasks')
def tasks():
    """Show all recent tasks from the cluster"""
    return render_template('tasks.html')

@app.route('/create_vm', methods=['GET', 'POST'])
def create_vm():
    """Create new VM or container"""
    if request.method == 'POST':
        try:
            node = request.form.get('node')
            vm_type = request.form.get('type')
            creation_method = request.form.get('creation_method')
            name = request.form.get('name')
            vmid = request.form.get('vmid')
            storage = request.form.get('storage')
            disk_size = request.form.get('disk_size')
            bridge = request.form.get('bridge', 'vmbr0')  # Default to vmbr0 if not specified
            
            proxmox = get_proxmox_for_node(node)
            if not proxmox:
                flash('Invalid node selected', 'error')
                return redirect(url_for('create_vm'))
            
            if creation_method == 'template' and vm_type == 'qemu':
                # Clone from VM template
                template_vmid = request.form.get('template')
                if not template_vmid:
                    flash('Template must be selected', 'error')
                    return redirect(url_for('create_vm'))
                
                # Clone the VM
                proxmox.nodes(node).qemu(template_vmid).clone.post(
                    newid=vmid,
                    name=name,
                    full=1  # Full clone
                )
                flash(f'VM {name} cloned from template successfully', 'success')
                
            elif vm_type == 'qemu':
                # Create VM from scratch
                params = {
                    'vmid': vmid,
                    'name': name,
                    'memory': int(request.form.get('memory', 512)),
                    'cores': int(request.form.get('cores', 1)),
                    'sockets': int(request.form.get('sockets', 1)),
                    'cpu': 'host',
                    'net0': f'virtio,bridge={bridge}',
                    'boot': 'c',
                    'scsihw': 'virtio-scsi-pci'
                }
                
                # Add disk if storage is specified
                if storage:
                    params['scsi0'] = f"{storage}:{disk_size}"
                    params['bootdisk'] = 'scsi0'
                
                # Create the VM
                proxmox.nodes(node).qemu.create(**params)
                
            elif vm_type == 'lxc':
                # Create container
                ostemplate = request.form.get('ostemplate')
                if not ostemplate and creation_method != 'blank':
                    flash('OS Template must be selected', 'error')
                    return redirect(url_for('create_vm'))
                
                params = {
                    'vmid': vmid,
                    'hostname': name,
                    'memory': int(request.form.get('memory', 512)),
                    'cores': int(request.form.get('cores', 1)),
                    'net0': f'name=eth0,bridge={bridge},ip=dhcp'
                }
                
                # Add storage
                if storage:
                    params['rootfs'] = f"{storage}:{disk_size}"
                
                # Add template if specified
                if ostemplate:
                    params['ostemplate'] = ostemplate
                
                # Create the container
                proxmox.nodes(node).lxc.create(**params)
            
            flash(f'{vm_type.upper()} {name} created successfully', 'success')
            return redirect(url_for('vms'))
            
        except Exception as e:
            flash(f'Error creating VM: {e}', 'error')
    
    # Get all online nodes for the form
    nodes = [n['name'] for n in cluster_nodes if n.get('status') == 'online']
    
    return render_template('create_vm.html', nodes=nodes)

@app.route('/storages')
def storages():
    """List all storage"""
    all_storages = []
    processed_storages = set()  # Track processed storages to avoid duplicates
    
    # Try to get storage info from cluster endpoint first
    if proxmox_nodes:
        try:
            proxmox = next(iter(proxmox_nodes.values()))
            storages = proxmox.cluster.resources.get(type='storage')
            
            for storage in storages:
                storage_key = f"{storage['node']}-{storage['storage']}"
                if storage_key not in processed_storages:
                    processed_storages.add(storage_key)
                    if storage.get('maxdisk') and storage.get('maxdisk') > 0:
                        storage['used_percent'] = (storage.get('disk', 0) / storage['maxdisk']) * 100
                    else:
                        storage['used_percent'] = 0
                    all_storages.append(storage)
                    
        except Exception as e:
            print(f"Error getting cluster storage: {e}")
            # Fallback to node-by-node
            all_storages = get_storages_fallback()
    
    # Group shared storage by storage name
    grouped_storages = group_shared_storages(all_storages)
    return render_template('storages.html', storages=grouped_storages)

def get_storages_fallback():
    """Fallback method to get storage node by node"""
    all_storages = []
    processed_storages = set()
    
    for node_info in cluster_nodes:
        try:
            node_name = node_info['name']
            proxmox = node_info['connection']
            
            storages = proxmox.nodes(node_name).storage.get()
            for storage in storages:
                storage_key = f"{node_name}-{storage['storage']}"
                if storage_key not in processed_storages:
                    processed_storages.add(storage_key)
                    storage['node'] = node_name
                    if storage.get('total') and storage.get('total') > 0:
                        storage['used_percent'] = (storage.get('used', 0) / storage['total']) * 100
                    else:
                        storage['used_percent'] = 0
                    all_storages.append(storage)
        except Exception as e:
            print(f"Error getting storage from node {node_info['name']}: {e}")
    
    return all_storages

def group_shared_storages(all_storages):
    """Group storage by storage name and combine nodes only if shared=1"""
    storage_groups = defaultdict(list)
    
    # Group storages by storage name
    for storage in all_storages:
        storage_name = storage.get('storage', '')
        storage_groups[storage_name].append(storage)
    
    grouped_storages = []
    
    for storage_name, storage_list in storage_groups.items():
        if not storage_list:
            continue
        
        # Check if this storage is shared (has shared=1 flag)
        is_shared = any(storage.get('shared', 0) == 1 for storage in storage_list)
        
        if is_shared and len(storage_list) > 1:
            # Combine shared storage across multiple nodes
            base_storage = storage_list[0].copy()
            nodes = []
            max_disk = 0
            max_maxdisk = 0
            enabled_count = 0
            
            for storage in storage_list:
                nodes.append(storage.get('node', ''))
                # For shared storage, use max values instead of sum (same storage seen from different nodes)
                if storage.get('disk'):
                    max_disk = max(max_disk, storage.get('disk', 0))
                if storage.get('maxdisk'):
                    max_maxdisk = max(max_maxdisk, storage.get('maxdisk', 0))
                if storage.get('enabled', 0) == 1 or storage.get('status') == 'available':
                    enabled_count += 1
            
            # Update the base storage with combined data
            base_storage['nodes'] = nodes
            base_storage['node_count'] = len(nodes)
            base_storage['disk'] = max_disk
            base_storage['maxdisk'] = max_maxdisk
            
            # Calculate usage percentage
            if max_maxdisk > 0:
                base_storage['used_percent'] = (max_disk / max_maxdisk) * 100
            else:
                base_storage['used_percent'] = 0
                
            # Storage is considered active if it's enabled on any node
            base_storage['is_active'] = enabled_count > 0
            
            grouped_storages.append(base_storage)
        else:
            # Add each storage separately (not shared or only on one node)
            for storage in storage_list:
                # Ensure individual storage has proper is_active flag
                storage['is_active'] = storage.get('enabled', 0) == 1 or storage.get('status') == 'available'
                grouped_storages.append(storage)
    
    return grouped_storages

@app.route('/networks')
def networks():
    """List all networks"""
    all_networks = []
    processed_networks = set()
    
    for node_info in cluster_nodes:
        try:
            node_name = node_info['name']
            proxmox = node_info['connection']
            
            networks = proxmox.nodes(node_name).network.get()
            for network in networks:
                network_key = f"{node_name}-{network['iface']}"
                if network_key not in processed_networks:
                    processed_networks.add(network_key)
                    network['node'] = node_name
                    all_networks.append(network)
        except Exception as e:
            print(f"Error getting networks from node {node_info['name']}: {e}")
    
    return render_template('networks.html', networks=all_networks)

@app.route('/cluster')
def cluster():
    """Show cluster information"""
    cluster_info = {
        'name': config['cluster']['name'],
        'nodes': []
    }
    
    # Get detailed status for each node
    for node_info in cluster_nodes:
        try:
            node_name = node_info['name']
            proxmox = node_info['connection']
            
            # Get node status
            status = proxmox.nodes(node_name).status.get()
            status['host'] = node_name
            status['online'] = True
            
            # Get additional node information
            try:
                # Get node subscription status if available
                subscription = proxmox.nodes(node_name).subscription.get()
                status['subscription'] = subscription
            except:
                pass
            
            cluster_info['nodes'].append(status)
            
        except Exception as e:
            cluster_info['nodes'].append({
                'host': node_info['name'],
                'error': str(e),
                'online': False
            })
    
    # Sort nodes by name
    cluster_info['nodes'].sort(key=lambda x: x['host'])
    
    return render_template('cluster.html', cluster=cluster_info)

@app.route('/api/nodes')
def api_nodes():
    """API endpoint to get all cluster nodes"""
    nodes = []
    for node_info in cluster_nodes:
        nodes.append({
            'name': node_info['name'],
            'status': node_info.get('status', 'unknown')
        })
    return jsonify(nodes)

@app.route('/api/resources')
def api_resources():
    """API endpoint to get all resources"""
    resources = get_all_vms_and_containers()
    return jsonify(resources)

@app.route('/api/node/<node>/status')
def api_node_status(node):
    """API endpoint to get specific node status"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    try:
        status = proxmox.nodes(node).status.get()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/node/<node>/storages')
def api_node_storages(node):
    """API endpoint to get storage pools for a specific node"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    # Get vm_type from query parameter (qemu or lxc)
    vm_type = request.args.get('vm_type', 'qemu')
    
    try:
        storages = proxmox.nodes(node).storage.get()
        # Filter storages based on VM type
        vm_storages = []
        for storage in storages:
            if storage.get('enabled', 0) == 1:
                content_types = storage.get('content', '').split(',')
                
                # Filter based on VM type
                is_suitable = False
                if vm_type == 'qemu' and 'images' in content_types:
                    is_suitable = True
                elif vm_type == 'lxc' and 'rootdir' in content_types:
                    is_suitable = True
                
                if is_suitable:
                    # Calculate available space
                    if storage.get('total') and storage.get('total') > 0:
                        storage['available_bytes'] = storage.get('total', 0) - storage.get('used', 0)
                        storage['available_gb'] = round(storage['available_bytes'] / (1024**3), 2)
                        storage['used_percent'] = (storage.get('used', 0) / storage['total']) * 100
                    else:
                        storage['available_bytes'] = 0
                        storage['available_gb'] = 0
                        storage['used_percent'] = 0
                    
                    vm_storages.append(storage)
            
        # Sort by available space (descending - most space first)
        vm_storages.sort(key=lambda x: x['available_bytes'], reverse=True)
        return jsonify(vm_storages)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/node/<node>/templates')
def api_node_templates(node):
    """API endpoint to get VM templates and container templates for a specific node"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    try:
        result = {
            'qemu': [],  # VM templates
            'lxc': []    # Container templates
        }
        
        # Get VM templates
        try:
            vms = proxmox.nodes(node).qemu.get()
            templates = [vm for vm in vms if vm.get('template') == 1]
            result['qemu'] = templates
        except Exception as e:
            print(f"Error getting VM templates: {e}")
        
        # Get container templates
        try:
            storages = proxmox.nodes(node).storage.get()
            for storage in storages:
                if 'vztmpl' in storage.get('content', '').split(','):
                    # Get templates in this storage
                    try:
                        templates = proxmox.nodes(node).storage(storage['storage']).content.get(content='vztmpl')
                        result['lxc'].extend(templates)
                    except Exception as e:
                        print(f"Error getting templates from storage {storage.get('storage')}: {e}")
        except Exception as e:
            print(f"Error getting LXC templates: {e}")
            
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/node/<node>/networks')
def api_node_networks(node):
    """API endpoint to get network interfaces for a specific node"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    try:
        networks = proxmox.nodes(node).network.get()
        # Filter for bridge interfaces
        bridges = [net for net in networks if net.get('type') == 'bridge']
        return jsonify(bridges)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cluster/nextid')
def api_next_vmid():
    """API endpoint to get the next available VMID"""
    if not proxmox_nodes:
        return jsonify({'error': 'No Proxmox connections available'}), 500
    
    try:
        # Get any working connection
        proxmox = next(iter(proxmox_nodes.values()))
        
        # Get next available VMID
        next_id = proxmox.cluster.nextid.get()
        return jsonify({'vmid': next_id})
    except Exception as e:
        return jsonify({'error': str(e), 'vmid': 100}), 500

@app.route('/api/vm/<node>/<vmid>/tasks')
def api_vm_tasks(node, vmid):
    """API endpoint to get recent tasks for a specific VM/container"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    try:
        # Get all tasks from the node
        all_tasks = proxmox.nodes(node).tasks.get()
        
        # Filter tasks related to this specific VM/container
        vm_tasks = []
        for task in all_tasks:
            # Check if task is related to this VMID
            task_description = task.get('type', '').lower()
            task_id = task.get('id', '')
            
            # Match tasks that contain the VMID or are VM-related operations
            if (vmid in task_id or 
                f":{vmid}:" in task_id or 
                task_id.endswith(f":{vmid}") or
                task_id.startswith(f"{vmid}:") or
                f"VMID {vmid}" in task.get('status', '') or
                (task.get('type') in ['qmstart', 'qmstop', 'qmshutdown', 'qmreset', 'qmigrate', 'qmclone', 'qmcreate', 'qmdestroy',
                                     'vzstart', 'vzstop', 'vzshutdown', 'vzmigrate', 'vzclone', 'vzcreate', 'vzdestroy'] and
                 vmid in str(task.get('id', '')))):
                
                # Add human-readable timestamps
                if task.get('starttime'):
                    task['start_time_formatted'] = datetime.fromtimestamp(task['starttime']).strftime('%Y-%m-%d %H:%M:%S')
                if task.get('endtime'):
                    task['end_time_formatted'] = datetime.fromtimestamp(task['endtime']).strftime('%Y-%m-%d %H:%M:%S')
                
                # Add status badge class for UI
                status = task.get('status', '').lower()
                if 'ok' in status or status == 'stopped':
                    task['status_class'] = 'success'
                elif 'error' in status or 'failed' in status:
                    task['status_class'] = 'danger'
                elif 'running' in status:
                    task['status_class'] = 'primary'
                else:
                    task['status_class'] = 'secondary'
                
                vm_tasks.append(task)
        
        # Sort by start time (most recent first)
        vm_tasks.sort(key=lambda x: x.get('starttime', 0), reverse=True)
        
        # Limit to last 10 tasks
        vm_tasks = vm_tasks[:10]
        
        return jsonify(vm_tasks)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tasks')
def api_cluster_tasks():
    """API endpoint to get all recent tasks from the cluster"""
    all_tasks = []
    processed_tasks = set()  # Track processed tasks to avoid duplicates
    
    # Collect tasks from all cluster nodes
    for node_info in cluster_nodes:
        try:
            node_name = node_info['name']
            proxmox = node_info['connection']
            
            # Get tasks from this node
            node_tasks = proxmox.nodes(node_name).tasks.get()
            
            for task in node_tasks:
                # Create unique task identifier
                task_key = f"{node_name}-{task.get('upid', task.get('id', ''))}"
                
                if task_key not in processed_tasks:
                    processed_tasks.add(task_key)
                    
                    # Add node information
                    task['node'] = node_name
                    
                    # Add human-readable timestamps
                    if task.get('starttime'):
                        task['start_time_formatted'] = datetime.fromtimestamp(task['starttime']).strftime('%Y-%m-%d %H:%M:%S')
                    if task.get('endtime'):
                        task['end_time_formatted'] = datetime.fromtimestamp(task['endtime']).strftime('%Y-%m-%d %H:%M:%S')
                    
                    # Add status badge class for UI
                    status = task.get('status', '').lower()
                    if 'ok' in status or status == 'stopped':
                        task['status_class'] = 'success'
                    elif 'error' in status or 'failed' in status:
                        task['status_class'] = 'danger'
                    elif 'running' in status:
                        task['status_class'] = 'primary'
                    else:
                        task['status_class'] = 'secondary'
                    
                    # Extract VMID from task if possible
                    task_id = task.get('id', '')
                    task_type = task.get('type', '')
                    if task_type in ['qmstart', 'qmstop', 'qmshutdown', 'qmreset', 'qmigrate', 'qmclone', 'qmcreate', 'qmdestroy',
                                   'vzstart', 'vzstop', 'vzshutdown', 'vzmigrate', 'vzclone', 'vzcreate', 'vzdestroy']:
                        # Try to extract VMID from task ID
                        import re
                        vmid_match = re.search(r':(\d+):', task_id)
                        if vmid_match:
                            task['vmid'] = vmid_match.group(1)
                    
                    all_tasks.append(task)
                    
        except Exception as e:
            print(f"Error getting tasks from node {node_info['name']}: {e}")
    
    # Sort by start time (most recent first)
    all_tasks.sort(key=lambda x: x.get('starttime', 0), reverse=True)
    
    # Get limit from query parameter, default to 50
    limit = request.args.get('limit', 50, type=int)
    all_tasks = all_tasks[:limit]
    
    return jsonify(all_tasks)

@app.route('/api/vm/<node>/<vmid>/metrics')
def api_vm_metrics(node, vmid):
    """API endpoint to get historical metrics for a specific VM/container"""
    proxmox = get_proxmox_for_node(node)
    if not proxmox:
        return jsonify({'error': 'Node not found'}), 404
    
    # Get timeframe from query parameter, default to 'day'
    timeframe = request.args.get('timeframe', 'day')
    
    # Map timeframes to Proxmox RRD timeframes
    timeframe_mapping = {
        'hour': 'hour',
        'day': 'day', 
        'week': 'week',
        'month': 'month',
        'year': 'year'
    }
    
    proxmox_timeframe = timeframe_mapping.get(timeframe, 'day')
    
    try:
        # Determine VM type
        vm_type = 'qemu'
        try:
            proxmox.nodes(node).qemu(vmid).config.get()
        except:
            vm_type = 'lxc'
        
        # Get current status for current values
        if vm_type == 'qemu':
            status = proxmox.nodes(node).qemu(vmid).status.current.get()
            # Get historical RRD data
            try:
                rrd_data = proxmox.nodes(node).qemu(vmid).rrddata.get(timeframe=proxmox_timeframe)
            except:
                rrd_data = []
        else:
            status = proxmox.nodes(node).lxc(vmid).status.current.get()
            # Get historical RRD data for containers
            try:
                rrd_data = proxmox.nodes(node).lxc(vmid).rrddata.get(timeframe=proxmox_timeframe)
            except:
                rrd_data = []
        
        # Process historical data for charts
        chart_data = {
            'labels': [],
            'cpu': [],
            'memory': [],
            'network_in': [],
            'network_out': [],
            'disk_read': [],
            'disk_write': []
        }
        
        max_memory = status.get('maxmem', 1)
        
        for entry in rrd_data:
            timestamp = entry.get('time', 0)
            if timestamp:
                # Format timestamp based on timeframe
                dt = datetime.fromtimestamp(timestamp)
                if timeframe == 'hour':
                    label = dt.strftime('%H:%M')
                elif timeframe == 'day':
                    label = dt.strftime('%H:%M')
                elif timeframe == 'week':
                    label = dt.strftime('%m/%d %H:%M')
                elif timeframe == 'month':
                    label = dt.strftime('%m/%d')
                else:  # year
                    label = dt.strftime('%Y-%m')
                
                chart_data['labels'].append(label)
                chart_data['cpu'].append((entry.get('cpu', 0) * 100) if entry.get('cpu') is not None else 0)
                chart_data['memory'].append((entry.get('mem', 0) / max_memory * 100) if entry.get('mem') and max_memory else 0)
                chart_data['network_in'].append(entry.get('netin', 0) / (1024*1024) if entry.get('netin') else 0)  # Convert to MB
                chart_data['network_out'].append(entry.get('netout', 0) / (1024*1024) if entry.get('netout') else 0)  # Convert to MB
                chart_data['disk_read'].append(entry.get('diskread', 0) / (1024*1024) if entry.get('diskread') else 0)  # Convert to MB
                chart_data['disk_write'].append(entry.get('diskwrite', 0) / (1024*1024) if entry.get('diskwrite') else 0)  # Convert to MB
        
        # Current metrics for display
        current_metrics = {
            'vm_type': vm_type,
            'status': status.get('status', 'unknown'),
            'uptime': status.get('uptime', 0),
            'cpu': {
                'current': status.get('cpu', 0) * 100 if status.get('cpu') is not None else 0,
                'cores': status.get('cpus', status.get('maxcpu', 1)),
            },
            'memory': {
                'used': status.get('mem', 0),
                'max': status.get('maxmem', 0),
                'usage_percent': (status.get('mem', 0) / status.get('maxmem', 1)) * 100 if status.get('maxmem') else 0,
                'used_gb': status.get('mem', 0) / (1024**3) if status.get('mem') else 0,
                'max_gb': status.get('maxmem', 0) / (1024**3) if status.get('maxmem') else 0
            },
            'disk': {
                'used': status.get('disk', 0),
                'max': status.get('maxdisk', 0),
                'usage_percent': (status.get('disk', 0) / status.get('maxdisk', 1)) * 100 if status.get('maxdisk') else 0,
                'used_gb': status.get('disk', 0) / (1024**3) if status.get('disk') else 0,
                'max_gb': status.get('maxdisk', 0) / (1024**3) if status.get('maxdisk') else 0
            },
        }
        
        response = {
            'current': current_metrics,
            'historical': chart_data,
            'timeframe': timeframe,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        return jsonify(response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    

@app.errorhandler(404)
def not_found(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    print("Initializing Proxmox connections...")
    init_proxmox_connections()
    
    if not proxmox_nodes:
        print("WARNING: No Proxmox connections established!")
    else:
        print(f"Successfully connected to {len(proxmox_nodes)} Proxmox nodes")
        print(f"Cluster contains {len(cluster_nodes)} total nodes")
    
    app.run(debug=True, host='0.0.0.0', port=8080)