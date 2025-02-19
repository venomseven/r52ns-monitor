import boto3
import json
import socket
import datetime
import os
from typing import Dict, List, Optional
import sys
import requests
import traceback
from flask import Flask, request, jsonify
import threading
import time
from dataclasses import dataclass
import yaml

app = Flask(__name__)
monitor = None  # Global variable to store monitor instance

@app.route('/slack/interactions', methods=['POST'])
def handle_slack_interaction():
    """Handle Slack button clicks"""
    print("🔄 Received Slack interaction")
    
    try:
        # Handle form-encoded data from Slack
        if not request.form.get('payload'):
            print("❌ No payload received")
            return jsonify({'error': 'No payload received'}), 400

        payload = json.loads(request.form.get('payload'))
        print(f"✅ Received payload structure: {json.dumps(payload, indent=2)}")
            
        if payload.get('type') == 'block_actions':
            action = payload['actions'][0]
            if action['action_id'] == 'resolve_nameserver_change':
                print("✅ Resolve button clicked")
                
                # Get the domain from the message attachments
                message = payload.get('message', {})
                attachments = message.get('attachments', [])
                domain = None
                
                for attachment in attachments:
                    for block in attachment.get('blocks', []):
                        if block.get('fields'):
                            for field in block['fields']:
                                if '*Domain:*' in field.get('text', ''):
                                    domain = field['text'].split('\n')[1].strip()
                                    break
                
                if domain:
                    print(f"🔍 Found domain: {domain}")
                    resolution_payload = {
                        "attachments": [
                            {
                                "color": "#28A745",  # Green color for success
                                "blocks": [
                                    {
                                        "type": "header",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "✅ Nameserver Recovery Detected",
                                            "emoji": True
                                        }
                                    },
                                    {
                                        "type": "section",
                                        "fields": [
                                            {
                                                "type": "mrkdwn",
                                                "text": f"*Domain:*\n{domain}"
                                            },
                                            {
                                                "type": "mrkdwn",
                                                "text": "*Status:*\nAll nameserver configurations have been verified and updated."
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                    
                    try:
                        webhook_url = monitor.config['slack']['webhooks']['prod']
                        print(f"🔄 Sending resolution message to {webhook_url}")
                        response = requests.post(webhook_url, json=resolution_payload)
                        print(f"📤 Slack response status: {response.status_code}")
                        
                        if response.status_code != 200:
                            print(f"❌ Error sending resolution message for {domain}: {response.status_code}")
                            print(f"Response: {response.text}")
                        else:
                            print(f"✅ Sent resolution message for {domain}")
                            
                        return jsonify({
                            "response_type": "in_channel",
                            "delete_original": False,
                            "text": "✅ Resolution processed successfully"
                        }), 200
                            
                    except Exception as e:
                        print(f"❌ Error sending resolution message: {e}")
                        print(traceback.format_exc())
                        return jsonify({'error': str(e)}), 500
                else:
                    print("❌ Could not find domain in message")
                    print(f"Message structure: {json.dumps(message, indent=2)}")
                    return jsonify({'error': 'Domain not found'}), 400
                
    except Exception as e:
        print(f"❌ Error handling Slack interaction: {e}")
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Unknown action'}), 400

def start_flask_server():
    """Start the Flask server"""
    try:
        print("Starting Flask server...")
        app.run(host='0.0.0.0', port=3000)
    except Exception as e:
        print(f"Error starting Flask server: {e}")
        print(traceback.format_exc())

@dataclass
class HostedZone:
    name: str
    description: str
    alert_channel: str
    priority: str
    environment: str
    check_frequency: int

class Route53NameserverMonitor:
    def __init__(self):
        """Initialize the monitor"""
        try:
            print("Initializing Route53 Nameserver Monitor...")
            
            # Load config
            self.config = self.load_config()
            print("✅ Loaded configuration")
            
            # Check where credentials are coming from
            session = boto3.Session()
            credentials = session.get_credentials()
            
            if credentials:
                print(f"🔑 AWS Credentials found from: {credentials.method}")
                print(f"🔑 Using AWS Access Key ID: {credentials.access_key[:5]}...")
                if 'AWS_ACCESS_KEY_ID' in os.environ:
                    print("📝 Using credentials from environment variables")
                elif os.path.exists(os.path.expanduser('~/.aws/credentials')):
                    print("📝 Using credentials from AWS CLI configuration")
            else:
                print("⚠️ No AWS credentials found!")
            
            # Initialize AWS client
            self.route53_client = boto3.client('route53')
            print("✅ Initialized AWS client")
            
            # Initialize zones
            self.zones = self.initialize_zones()
            print(f"✅ Initialized {len(self.zones)} zones")
            
            # Set up history file
            self.history_file = os.path.join('data', 'nameserver_history.json')
            print(f"✅ History file path: {self.history_file}")
            
            # Initialize monitoring threads dictionary
            self.monitoring_threads = {}
            self.stop_monitoring = False
            
        except Exception as e:
            print(f"❌ Error initializing monitor: {e}")
            print(traceback.format_exc())
            raise

    def initialize_zones(self) -> List[HostedZone]:
        """Initialize hosted zone objects from config"""
        zones = []
        try:
            # Get default frequencies from config
            default_frequencies = self.config['monitoring']['frequencies']
            
            for env, zone_list in self.config['hosted_zones'].items():
                # Get default frequency for this environment
                default_freq = default_frequencies.get(env, 300)  # Default to 5 minutes if not specified
                
                for zone in zone_list:
                    # Use zone-specific frequency if provided, otherwise use environment default
                    frequency = zone.get('check_frequency', default_freq)
                    print(f"🕒 Zone {zone['name']} frequency: {frequency} seconds")
                    
                    zones.append(HostedZone(
                        name=zone['name'],
                        description=zone['description'],
                        alert_channel=zone.get('alert_channel', self.config['slack']['default_channel']),
                        priority=zone.get('priority', 'medium'),
                        environment=env,
                        check_frequency=frequency
                    ))
                    
            return zones
        except Exception as e:
            print(f"❌ Error initializing zones: {e}")
            print(traceback.format_exc())
            raise

    def monitor_zone(self, zone: HostedZone):
        """Monitor a specific zone at its configured frequency"""
        print(f"🔄 Starting monitoring for {zone.name} (checking every {zone.check_frequency} seconds)")
        
        while not self.stop_monitoring:
            try:
                print(f"\n🔍 Checking zone: {zone.name}")
                current_state = self.get_zone_nameserver_ips(zone)
                
                if current_state:
                    print(f"✅ Got current state for {zone.name}")
                    changes = self.check_for_changes(current_state)
                    if changes:
                        print(f"🚨 Found changes for {zone.name}")
                        self.send_slack_notification(changes)
                    else:
                        print(f"✅ No changes detected for {zone.name}")
                    
                    self.save_current_state(current_state)
                else:
                    print(f"❌ No state retrieved for {zone.name}")
                
                # Sleep for the zone-specific frequency
                print(f"💤 {zone.name}: Sleeping for {zone.check_frequency} seconds...")
                time.sleep(zone.check_frequency)
                
            except Exception as e:
                print(f"❌ Error monitoring {zone.name}: {e}")
                print(traceback.format_exc())
                time.sleep(60)  # Wait a minute before retrying on error

    def start_monitoring(self):
        """Start monitoring threads for all zones"""
        try:
            print("Starting monitoring threads...")
            for zone in self.zones:
                thread = threading.Thread(
                    target=self.monitor_zone,
                    args=(zone,),
                    name=f"monitor-{zone.name}"
                )
                thread.daemon = True
                thread.start()
                self.monitoring_threads[zone.name] = thread
                print(f"✅ Started monitoring thread for {zone.name} (frequency: {zone.check_frequency}s)")
                
        except Exception as e:
            print(f"❌ Error starting monitoring: {e}")
            print(traceback.format_exc())
            raise

    def get_zone_nameserver_ips(self, zone: HostedZone) -> Dict[str, Dict]:
        """Collect current nameserver IPs from Route53"""
        print(f"Getting nameserver IPs for {zone.name}")
        delegation_sets = {}
        api_calls = 0
        
        try:
            api_calls += 1
            hosted_zones = self.route53_client.list_hosted_zones()
            print(f"API Call #{api_calls}: list_hosted_zones")
            
            for hz in hosted_zones['HostedZones']:
                zone_name = hz['Name'].rstrip('.')
                
                if zone_name == zone.name:
                    zone_id = hz['Id']
                    api_calls += 1
                    zone_details = self.route53_client.get_hosted_zone(Id=zone_id)
                    print(f"API Call #{api_calls}: get_hosted_zone for {zone_name}")
                    nameservers = zone_details['DelegationSet']['NameServers']
                    
                    nameservers_info = {}
                    for ns in nameservers:
                        try:
                            # Get both IPv4 and IPv6 addresses
                            ipv4_ips = []
                            ipv6_ips = []
                            
                            # Get IPv4 addresses
                            try:
                                ipv4_info = socket.getaddrinfo(ns, None, socket.AF_INET)
                                ipv4_ips = list(set(info[4][0] for info in ipv4_info))
                                print(f"IPv4 addresses for {ns}: {ipv4_ips}")
                            except socket.gaierror as e:
                                print(f"No IPv4 addresses found for {ns}: {e}")
                                
                            # Get IPv6 addresses
                            try:
                                ipv6_info = socket.getaddrinfo(ns, None, socket.AF_INET6)
                                ipv6_ips = list(set(info[4][0] for info in ipv6_info))
                                print(f"IPv6 addresses for {ns}: {ipv6_ips}")
                            except socket.gaierror as e:
                                print(f"No IPv6 addresses found for {ns}: {e}")
                                
                            nameservers_info[ns] = {
                                'ipv4': ipv4_ips,
                                'ipv6': ipv6_ips
                            }
                        except Exception as e:
                            print(f"Error resolving {ns}: {e}")
                            nameservers_info[ns] = {'ipv4': [], 'ipv6': []}
                    
                    delegation_sets[zone_id] = {
                        "zone_name": zone_name,
                        "nameservers": nameservers_info
                    }
                    
            return delegation_sets
            
        except Exception as e:
            print(f"❌ Error getting nameserver information: {e}")
            print(traceback.format_exc())
            return {}
            
    def load_history(self, history_file: str) -> Dict:
        """Load previous nameserver data from JSON file"""
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {"history": []}
        return {"history": []}
    
    def _apply_retention_policy(self, history: Dict):
        """Apply retention policies to history"""
        if not history["history"]:
            return
            
        current_time = datetime.datetime.now()
        retained_entries = []
        
        for entry in history["history"]:
            entry_time = datetime.datetime.fromisoformat(entry["timestamp"])
            age_days = (current_time - entry_time).days
            
            # Get retention settings from config
            max_days = self.config.get('monitoring', {}).get('retention_days', 30)
            max_entries = self.config.get('monitoring', {}).get('retention_entries', 1000)
            
            if age_days <= max_days:
                retained_entries.append(entry)
        
        # Keep only the most recent entries if we exceed max_entries
        if len(retained_entries) > max_entries:
            retained_entries = retained_entries[-max_entries:]
        
        history["history"] = retained_entries

    def save_current_state(self, current_state: Dict):
        """Save current state to history file"""
        try:
            print(f"Creating directory: {os.path.dirname(self.history_file)}")
            os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
            
            history = self.load_history(self.history_file)
            
            # Add new entry
            entry = {
                "timestamp": datetime.datetime.now().isoformat(),
                "delegation_sets": current_state
            }
            
            # Compare states with detailed logging
            if not history.get("history"):
                print("First entry in history")
                history["history"] = [entry]
            else:
                last_state = history["history"][-1]["delegation_sets"]
                state_changed = False
                
                # Compare each zone separately
                for zone_id, current_info in current_state.items():
                    if zone_id in last_state:
                        if last_state[zone_id] != current_info:
                            print(f"🚨 Changes detected for {current_info['zone_name']}:")
                            print(f"Previous state: {json.dumps(last_state[zone_id], indent=2)}")
                            print(f"Current state: {json.dumps(current_info, indent=2)}")
                            state_changed = True
                    else:
                        print(f"🆕 New zone detected: {current_info['zone_name']}")
                        state_changed = True
                
                if state_changed:
                    print("💾 Saving new state due to changes")
                    history["history"].append(entry)
                else:
                    print("✅ No changes detected, skipping save")
                    return
            
            # Apply retention policies
            self._apply_retention_policy(history)
            
            with open(self.history_file, 'w') as f:
                json.dump(history, f, indent=2)
            
        except Exception as e:
            print(f"❌ Error saving state: {e}")
            print(traceback.format_exc())

    def check_for_changes(self, current_state: Dict) -> List[Dict]:
        """Check for changes in nameserver IPs"""
        changes = []
        try:
            history = self.load_history(self.history_file)
            last_state = history.get("history", [])[-1] if history.get("history") else None
            
            if not last_state:
                print("⚠️ No previous state found, establishing baseline")
                return []
            
            last_delegation_sets = last_state["delegation_sets"]
            
            for zone_id, current_info in current_state.items():
                if zone_id not in last_delegation_sets:
                    print(f"⚠️ New zone detected: {current_info['zone_name']}")
                    continue
                
                last_info = last_delegation_sets[zone_id]
                
                for ns, current_ips in current_info["nameservers"].items():
                    last_ips = last_info["nameservers"].get(ns, {'ipv4': [], 'ipv6': []})
                    
                    # Check both IPv4 and IPv6 changes
                    if (current_ips.get('ipv4') != last_ips.get('ipv4') or 
                        current_ips.get('ipv6') != last_ips.get('ipv6')):
                        print(f"🚨 IP change detected for {ns} in {current_info['zone_name']}")
                        print(f"Previous state: {last_ips}")
                        print(f"Current state: {current_ips}")
                        
                        changes.append({
                            "type": "ip_change",
                            "zone_name": current_info["zone_name"],
                            "delegation_set": zone_id.split("/")[-1],
                            "nameserver": ns,
                            "old_ips": last_ips,
                            "new_ips": current_ips
                        })
            
            if changes:
                print(f"🚨 Found {len(changes)} changes")
            else:
                print("✅ No changes detected")
            
            return changes
            
        except Exception as e:
            print(f"❌ Error checking for changes: {e}")
            print(traceback.format_exc())
            return []

    def simulate_changes(self):
        """Test function to simulate changes"""
        print("Starting simulation...")
        all_changes = {}
        
        for zone in self.zones:
            print(f"\nTesting zone: {zone.name}")
            current_state = self.get_zone_nameserver_ips(zone)
            
            if not current_state:
                print(f"Error: No current state found for {zone.name}")
                continue
            
            zone_id = list(current_state.keys())[0]
            print(f"Found zone ID: {zone_id}")
            
            # Simulate new IP
            ns = list(current_state[zone_id]['nameservers'].keys())[0]
            print(f"Modifying nameserver: {ns}")
            
            # Store original state with example IPs
            original_state = {
                'ipv4': ['205.251.195.19'],
                'ipv6': []
            }
            
            # Simulate new state with both IPv4 and IPv6 changes
            new_state = {
                'ipv4': ['9.10.11.12'],
                'ipv6': ['2001:db8::3']
            }
            
            print(f"Original state: {original_state}")
            current_state[zone_id]['nameservers'][ns] = new_state
            print(f"Modified state: {new_state}")
            
            # Create the change record
            changes = [{
                "type": "ip_change",
                "zone_name": current_state[zone_id]['zone_name'],
                "delegation_set": zone_id.split("/")[-1],
                "nameserver": ns,
                "old_ips": original_state,
                "new_ips": new_state
            }]
            
            # Send test notification
            self.send_slack_notification(changes)
            
            all_changes[zone_id] = current_state[zone_id]
        
        return all_changes

    def send_slack_notification(self, changes: List[Dict]):
        """Send notification to Slack"""
        try:
            webhook_url = self.config['slack']['webhooks']['prod']
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            for change in changes:
                if change["type"] == "ip_change":
                    # Format IPs more compactly
                    old_ips_str = str(change['old_ips']).replace(' ', '')
                    new_ips_str = str(change['new_ips']).replace(' ', '')
                    
                    payload = {
                        "attachments": [
                            {
                                "color": "#FF0000",
                                "blocks": [
                                    {
                                        "type": "header",
                                        "text": {
                                            "type": "plain_text",
                                            "text": "🚨 Route53 Nameserver Alert! 🚨",
                                            "emoji": True
                                        }
                                    },
                                    {
                                        "type": "section",
                                        "fields": [
                                            {
                                                "type": "mrkdwn",
                                                "text": f"*Domain:*\n{change['zone_name']}"
                                            },
                                            {
                                                "type": "mrkdwn",
                                                "text": f"*Detection Time:*\n{current_time}"
                                            }
                                        ]
                                    },
                                    {
                                        "type": "section",
                                        "text": {
                                            "type": "mrkdwn",
                                            "text": f"📝 *Nameserver IP Change*\n*Zone:* {change['zone_name']}\n*ID:* `/hostedzone/{change['delegation_set']}`\n*Nameserver:* `{change['nameserver']}`"
                                        }
                                    },
                                    {
                                        "type": "section",
                                        "fields": [
                                            {
                                                "type": "mrkdwn",
                                                "text": f"*Previous IPs:*\n`{old_ips_str}`"
                                            },
                                            {
                                                "type": "mrkdwn",
                                                "text": f"*New IPs:*\n`{new_ips_str}`"
                                            }
                                        ]
                                    },
                                    {
                                        "type": "context",
                                        "elements": [
                                            {
                                                "type": "mrkdwn",
                                                "text": "🔍 Route53 Nameserver Monitor"
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                "color": "#FF0000",
                                "blocks": [
                                    {
                                        "type": "actions",
                                        "elements": [
                                            {
                                                "type": "button",
                                                "text": {
                                                    "type": "plain_text",
                                                    "text": "✅ Resolve",
                                                    "emoji": True
                                                },
                                                "style": "primary",
                                                "action_id": "resolve_nameserver_change"
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                    
                    response = requests.post(webhook_url, json=payload)
                    if response.status_code != 200:
                        print(f"❌ Error sending Slack notification. Status code: {response.status_code}")
                        print(f"Response: {response.text}")
                
        except Exception as e:
            print(f"❌ Error sending Slack notification: {e}")
            print(traceback.format_exc())

    def test_slack_webhook(self):
        """Test the Slack webhook connection"""
        webhook_url = self.config['slack']['webhooks']['prod']
        if not webhook_url:
            print("No Slack webhook URL configured!")
            return False
            
        try:
            test_payload = {
                "text": "🔍 *Route53 Monitor Test*\n\nThis is a test message to verify Slack notifications are working."
            }
            
            print(f"\nTesting Slack webhook connection...")
            print(f"Webhook URL: {webhook_url}")
            
            response = requests.post(webhook_url, json=test_payload)
            print(f"Response Status: {response.status_code}")
            print(f"Response Text: {response.text}")
            
            if response.status_code == 200:
                print("✅ Slack webhook test successful!")
                return True
            else:
                print("❌ Slack webhook test failed!")
                return False
                
        except Exception as e:
            print(f"Error testing Slack webhook: {e}")
            print(traceback.format_exc())
            return False

    def load_config(self) -> Dict:
        """Load and validate configuration"""
        try:
            with open('config.yaml', 'r') as f:
                config = yaml.safe_load(f)
                
            # Validate required configuration
            required_keys = ['monitoring', 'hosted_zones', 'slack']
            for key in required_keys:
                if key not in config:
                    raise ValueError(f"Missing required config section: {key}")
                    
            print(f"✅ Loaded configuration from config.yaml")
            return config
                
        except Exception as e:
            print(f"❌ Error loading config from config.yaml: {e}")
            raise

def notify_changes(changes: List[Dict], monitor: Route53NameserverMonitor):
    """Notify about changes with enhanced formatting"""
    if not changes:
        return
        
    for change in changes:
        if change["type"] == "ip_change":
            message = (
                "📝 *Nameserver IP Change*\n\n"
                f"*Zone:* {change['zone_name']}\n"
                f"*ID:* `/hostedzone/{change['delegation_set']}`\n"
                f"*Nameserver:* `{change['nameserver']}`\n\n"
                "*Previous IPs:*\n"
                f"`{', '.join(change['old_ips'])}`\n\n"
                "*New IPs:*\n"
                f"`{', '.join(change['new_ips'])}`"
            )
            
            # Send alert with resolve button
            monitor.send_slack_notification([change])

def main():
    try:
        print("Starting Route53 Nameserver Monitor...")
        global monitor
        monitor = Route53NameserverMonitor()
        
        # Start Flask server in a separate thread
        flask_thread = threading.Thread(target=start_flask_server)
        flask_thread.daemon = False  # Changed to non-daemon
        flask_thread.start()
        print("✅ Flask server started on port 3000")
        
        if len(sys.argv) > 1 and sys.argv[1] == "--test":
            print("\n🧪 Running in test mode...")
            time.sleep(2)  # Give Flask time to start
            current_state = monitor.simulate_changes()
            if current_state:
                print("\n🔍 Checking for changes in simulated state...")
                changes = monitor.check_for_changes(current_state)
                if changes:
                    print(f"🚨 Found {len(changes)} simulated changes")
                    monitor.send_slack_notification(changes)
                else:
                    print("❌ No changes detected in simulation")
            print("\n✅ Test completed")
            print("\n🔄 Keeping server running for testing resolve functionality...")
            print("Press Ctrl+C to exit")
            
            # Keep the main thread alive
            while True:
                time.sleep(1)
        else:
            # Start monitoring for all zones
            monitor.start_monitoring()
            print("\n✅ Monitoring started for all zones")
            
            # Keep the main thread alive
            while True:
                time.sleep(1)
                
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
        if monitor:
            monitor.stop_monitoring = True
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        print(traceback.format_exc())

if __name__ == "__main__":
    main()