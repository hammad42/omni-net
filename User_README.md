# User Guide: OmniNet Multi-WAN Engine

Welcome to OmniNet! This application allows you to combine, manage, and optimize multiple internet connections (like your Ethernet, Wi-Fi, and a USB tethered connection) on your Windows PC. 

This guide will teach you how to use OmniNet and configure its options to best suit your specific needs, whether you are gaming, downloading massive files, or just need a rock-solid connection for streaming.

---

## 1. Getting Started

When you start OmniNet, you will see your available network adapters (e.g., Ethernet, Wi-Fi). OmniNet works by intelligently routing your computer's internet traffic across these adapters based on the **Mode** you select.

### The Three Main Modes
You can change how OmniNet behaves by switching the mode in the interface or modifying the configuration file.

1. **Auto (Failover & Lowest Latency)** 
   * **Best for:** Gaming and Video Calls.
   * **How it works:** OmniNet constantly tests all your connections. It will route all your general traffic through the connection with the lowest ping (latency). If that line drops or fails, OmniNet will instantly switch your traffic to the next best line in under 500 milliseconds.
2. **Aggregate (Combine Connections)**
   * **Best for:** Torrenting, Browsing, and running multiple apps.
   * **How it works:** OmniNet will distribute your traffic across all healthy internet lines simultaneously. If you open a web page, some images download over your Wi-Fi, and some over Ethernet, effectively combining their speeds.
3. **Force (Pin to Adapter)**
   * **Best for:** Testing a specific connection.
   * **How it works:** Forces all your traffic through one specific adapter, ignoring the others.

---

## 2. Tailoring OmniNet to Your Use Case

You can fine-tune OmniNet by adjusting settings in the User Interface or by editing the configuration file located at `%APPDATA%\OmniNet\config.json`.

### Use Case A: The Hardcore Gamer (Zero Lag & Failover)
If you are playing competitive games (like Valorant or CS2), you don't want connections combining; you want pure stability.
1. Set the Mode to **Auto**.
2. **Set App Rules**: Go to the settings and create a rule for your game (e.g., `valorant.exe`). Set its target to your most stable connection (e.g., `Ethernet`).
3. Now, your game will always use the stable wired connection, while OmniNet can freely route background downloads (like Discord updates) over your Wi-Fi.

### Use Case B: The Heavy Downloader (Maximum Speed)
If you want to download a massive file as fast as possible using both your Ethernet and Wi-Fi:
1. Set the Mode to **Aggregate**.
2. **Adjust Weights**: In the configuration, you can assign "Weights" to your adapters. If your Ethernet is 1 Gbps and your Wi-Fi is 100 Mbps, give Ethernet a weight of `100` and Wi-Fi a weight of `10`. This tells OmniNet to send 10 times more traffic to the Ethernet so the Wi-Fi doesn't bottleneck.
3. **Use Turbo Download**: For a single large file, use the built-in "Turbo Download" tab. Paste the URL here, and OmniNet will chop the file into small chunks, pulling them simultaneously over all available connections for true speed aggregation.

### Use Case C: The Remote Worker (Never Drop a Call)
If you are on Zoom/Teams and your primary ISP is unreliable:
1. Connect a 4G/5G mobile hotspot via USB or Wi-Fi.
2. Set Mode to **Auto**.
3. In your settings, ensure the **Fail Threshold** is low (e.g., `2`). This means if OmniNet misses two health checks on your main line, it will instantly move your Zoom call's traffic to the mobile hotspot.

---

## 3. How to Route Apps via the Proxy

For OmniNet to aggregate connections perfectly, you should tell your heavy applications to use OmniNet's local proxy. 

* **Web Browsers (Chrome/Edge):** Launch the browser with the command line argument `--proxy-server="socks5://127.0.0.1:1080"`
* **Firefox:** Go to Settings -> Network Settings -> Manual Proxy Configuration. Enter `127.0.0.1` for SOCKS Host and `1080` for Port. Check "Proxy DNS when using SOCKS v5".
* **Everything Else:** Enable **Transparent Mode** in OmniNet settings. OmniNet will automatically capture traffic from apps and balance them.

## 4. Advanced Settings (`config.json`)

If you want to edit `%APPDATA%\OmniNet\config.json` manually, here are the key options you can change:

* `"probe_interval_s"`: How often OmniNet checks if your internet is working (Default is `3.0` seconds). Lower this for faster detection, but it uses more background data.
* `"fail_threshold"`: How many failed checks before a connection is considered "Dead".
* `"rtt_switch_threshold_ms"`: Prevents OmniNet from constantly switching between two lines if their ping is very similar. (Default `130.0` ms).

**Need to reset?** If you ever mess up the configuration, simply delete `config.json` and restart OmniNet. It will generate a fresh default setup.
