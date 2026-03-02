#!/usr/bin/env python3
import asyncio
import os
import sys
import logging
import re
from datetime import datetime
from bleak import BleakScanner

# --- Configuration ---
WORK_DIR = "/opt/drone_updater/"
CONFIG_DIR = "/boot/firmware/drone_updater/"

MAPPING_FILE = os.path.join(CONFIG_DIR, "firmware_mapping.txt")
DFU_MAPPING_FILE = os.path.join(CONFIG_DIR, "dfu_mapping.txt")

OVERRIDE_FW = os.path.join(CONFIG_DIR, "firmware.zip")
DFU_OVERRIDE_FW = os.path.join(CONFIG_DIR, "dfu.zip")

DEVICES_LOG = os.path.join(CONFIG_DIR, "devices_found.txt")
LOG_FILE = "/var/log/drone_updater.log"
DFU_SCRIPT = os.path.join(WORK_DIR, "dfu_cli.py")

PRN_VALUE = "8"
RETRY_N = "5"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB
extra_params = "--scan"
high_mtu = "--high-mtu"

# --- Global State ---
found_devices = {}  # Thread-safe dictionary for main loop to check

# --- Configure Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger('').addHandler(console)

def log_device_to_file(device, advertisement_data):
    """Logs scanned devices and prunes the file if it exceeds 10MB."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name = device.name or "Unknown"
        address = device.address
        rssi = advertisement_data.rssi
        line = f"{timestamp}, {name}, {address}, {rssi}\n"
        # Append the new entry
        with open(DEVICES_LOG, "a") as f:
            f.write(line)

        # Check size and prune if over 10MB
        if os.path.getsize(DEVICES_LOG) > MAX_LOG_SIZE:
            with open(DEVICES_LOG, "r") as f:
                lines = f.readlines()
            # Keep the most recent 50% of the lines to reduce file size immediately
            pruned_content = lines[len(lines)//2:]
            with open(DEVICES_LOG, "w") as f:
                f.writelines(pruned_content)
            logging.info(f"Log rotation: Pruned old entries from {os.path.basename(DEVICES_LOG)}")
    except Exception as e:
        logging.error(f"Error managing device log: {e}")

def detection_callback(device, advertisement_data):
    """Callback triggered by Bleak when a device is found."""
    if device.name:
        found_devices[device.name] = device
    log_device_to_file(device, advertisement_data)

async def wait_for_downloader():
    """Wait for the downloader service to finish to prevent file conflicts."""
    service_name = "firmware-downloader.service"
    while True:
        try:
            cmd = ["systemctl", "is-active", service_name]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if stdout.decode().strip() in ["active", "activating"]:
                logging.info("Downloader service active. Waiting...")
                await asyncio.sleep(5)
            else:
                break
        except:
            break

def load_mapping(mapping_file_path, force_override_path=None):
    """Loads device mappings, prioritizing .zip override files."""
    mapping = {}
    if not os.path.exists(mapping_file_path):
        return mapping

    active_override = force_override_path if (force_override_path and os.path.exists(force_override_path)) else None

    try:
        with open(mapping_file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split(None, 1)
                if len(parts) >= 1:
                    device_name = parts[0]
                    raw_path = active_override if active_override else (parts[1] if len(parts) == 2 else "")
                    if raw_path:
                        real_path = os.path.realpath(raw_path)
                        if os.path.exists(real_path):
                            mapping[device_name] = real_path
    except Exception as e:
        logging.error(f"Error reading mapping {mapping_file_path}: {e}")
    return mapping

async def run_dfu(target_name, address, firmware_path):
    """Executes the DFU process with real-time progress logging."""
    logging.info(f"STARTING OTA: {target_name} [{address}]")
    logging.info(f"FIRMWARE: {firmware_path}")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, DFU_SCRIPT, "--prn", PRN_VALUE, "--retry", RETRY_N, extra_params, high_mtu, firmware_path, address]
    cleanup_pattern = re.compile(r"^\d{2}:\d{2}:\d{2}\s+\[\w+\]\s+")
    percent_pattern = re.compile(r"(\d{1,3})\s?%")
    last_logged_percent = -1
    char_buffer = []

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
        )
        while True:
            char_bytes = await process.stdout.read(1)
            if not char_bytes: break
            char = char_bytes.decode('utf-8', errors='ignore')
            if char in ['\n', '\r']:
                line = "".join(char_buffer).strip()
                char_buffer = []
                if not line: continue
                match_pct = percent_pattern.search(line)
                if match_pct:
                    pct = int(match_pct.group(1))
                    if pct != last_logged_percent:
                        logging.info(f"DFU: Flashing Progress: {pct}%")
                        last_logged_percent = pct
                else:
                    clean_line = cleanup_pattern.sub("", line)
                    if any(k in line for k in ["Target", "Connect", "Jump", "Upload", "Success", "Error", "Exception", "Timeout", "Verifying", "Successful"]):
                        logging.info(f"DFU: {clean_line}")
            else:
                char_buffer.append(char)
                # Check buffer for progress even if no newline received
                match_pct = percent_pattern.search("".join(char_buffer))
                if match_pct:
                    pct = int(match_pct.group(1))
                    if pct != last_logged_percent:
                        logging.info(f"DFU: Flashing Progress: {pct}%")
                        last_logged_percent = pct

        await process.wait()
        if process.returncode == 0:
            logging.info(f"SUCCESS: Flashing finished for {target_name}")
            if firmware_path in [OVERRIDE_FW, DFU_OVERRIDE_FW] and os.path.exists(firmware_path):
                os.remove(firmware_path)
            return True
        else:
            logging.error(f"FAILED: Flashing ended with code {process.returncode}")
            return False
    except Exception as e:
        logging.error(f"Execution Exception: {e}")
        return False

async def service_loop():
    """Main loop: Maintains scanner and triggers DFU on matches."""
    await wait_for_downloader()
    logging.info("--- Drone Auto-Updater Service Started ---")

    scanner = BleakScanner(detection_callback=detection_callback)

    while True:
        try:
            # Start/Ensure scanner is running at the start of loop
            await scanner.start()
            logging.info("Scanning for devices...")

            target_found = None
            while not target_found:
                dfu_mapping = load_mapping(DFU_MAPPING_FILE, DFU_OVERRIDE_FW)
                standard_mapping = load_mapping(MAPPING_FILE, OVERRIDE_FW)

                # Check if any seen device matches our mapping
                current_names = list(found_devices.keys())
                for name in current_names:
                    if name in dfu_mapping:
                        target_found = (name, found_devices[name].address, dfu_mapping[name])
                        break
                    elif name in standard_mapping:
                        target_found = (name, found_devices[name].address, standard_mapping[name])
                        break
                if target_found:
                    break
                await asyncio.sleep(2) # Check mapping every 2 seconds
            # TARGET FOUND: Stop scanner for reliable flashing
            await scanner.stop()
            logging.info("Stopping scanner for DFU flash...")
            name, addr, path = target_found
            success = await run_dfu(name, addr, path)
            # Clear device cache so we don't immediately re-trigger on the same device
            found_devices.clear()
        except Exception as e:
            logging.error(f"Main Loop Error: {e}")
            try: await scanner.stop();
            except: pass
            await asyncio.sleep(5)

if __name__ == "__main__":
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        asyncio.run(service_loop())
    except KeyboardInterrupt:
        pass
