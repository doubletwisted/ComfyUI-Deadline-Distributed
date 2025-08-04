<div align="center">
<img width="250" src="https://github.com/user-attachments/assets/533bb98d-0c4a-499f-9bca-5c937e361087" />
<br><br>
<a href="https://www.youtube.com/watch?v=p6eE3IlAbOs"><img src="https://img.shields.io/badge/Video_Tutorial-grey?style=flat&logo=youtube&logoColor=white" alt="Video Tutorial"></a>
<a href="/docs/worker-setup-guides.md"><img src="https://img.shields.io/badge/Setup_Guides-grey?style=flat&logo=gitbook&logoColor=white" alt="Setup Guides"></a>
<a href="/workflows"><img src="https://img.shields.io/badge/Workflows-grey?style=flat&logo=json&logoColor=white" alt="Workflows"></a>
<a href="https://buymeacoffee.com/robertvoy"><img src="https://img.shields.io/badge/Donation-grey?style=flat&logo=buymeacoffee&logoColor=white" alt="Donation"></a>
<a href="https://x.com/robertvoy3D"><img src="https://img.shields.io/twitter/follow/robertvoy3D" alt="Twitter"></a>
<br><br>
</div>

> **A powerful extension for ComfyUI that enables distributed and parallel processing across multiple GPUs and machines, including Thinkbox Deadline render farm integration. Generate more images and videos and accelerate your upscaling workflows by leveraging all available GPU resources in your network, cloud, and render farm.**

![Clipboard Image (7)](https://github.com/user-attachments/assets/66aaadef-f195-48a1-a368-17dd0dae477d)

---

## Key Features

#### Parallel Workflow Processing
- Run your workflow on multiple GPUs simultaneously with varied seeds, collect results on the master
- Scale output with more workers
- Supports images and videos

#### Distributed Upscaling
- Accelerate Ultimate SD Upscale by distributing tiles across GPUs
- Intelligent distribution
- Handles single images and batches

#### Ease of Use
- Auto-setup local workers; easily add remote/cloud ones
- Convert any workflow to distributed with 2 nodes
- JSON configuration with UI controls

#### 🆕 Deadline Integration
- **Interactive Distributed Mode** - Claim Deadline workers for real-time distributed processing
- **Batch Render Mode** - Submit workflows to Deadline for overnight rendering
- **Enterprise Scale** - Leverage existing render farm infrastructure
- **Mixed Worker Pools** - Combine local, remote, cloud, and Deadline workers

---

## Worker Types

<img width="200" align="right" alt="ComfyUI_temp_khvcc_00034_@0 25x" src="https://github.com/user-attachments/assets/651e4912-7c23-4e32-bd88-250f5175e129" />

ComfyUI Distributed supports four types of workers:

- **Local Workers** - Additional GPUs on the same machine (auto-configured on first launch)
- **Remote Workers** - GPUs on other computers in your network
- **Cloud Workers** - GPUs hosted on a cloud service like Runpod, accessible via secure tunnels
- **🆕 Deadline Workers** - GPUs managed by Thinkbox Deadline render farm for enterprise-scale distributed processing

> For detailed setup instructions, see the [setup guide](/docs/worker-setup-guides.md)

---

## Requirements

- ComfyUI
> Note: Desktop app not currently supported
- Multiple NVIDIA GPUs
> No additional GPUs? Use [Cloud Workers](https://github.com/robertvoy/ComfyUI-Distributed/blob/main/docs/worker-setup-guides.md#cloud-workers)
- That's it

---

## Installation

1. **Clone this repository** into your ComfyUI custom nodes directory:
   ```bash
   git clone https://github.com/robertvoy/ComfyUI-Distributed.git
   ```
   
2. **Restart ComfyUI**
   - If you'll be using remote/cloud workers, add `--enable-cors-header` to your launch arguments on the master

3. Read the [setup guide](/docs/worker-setup-guides.md) for adding workers

---

## Official Sponsor

[<img width="1500" height="339" src="https://github.com/user-attachments/assets/c5f75e1f-3e19-4c57-b05d-151311cd1cf0" />](https://get.runpod.io/0bw29uf3ug0p)

Join Runpod with [this link](https://get.runpod.io/0bw29uf3ug0p) and unlock a special bonus.

---

## Workflow Examples

### Basic Parallel Generation
Generate multiple images in the time it takes to generate one. Each worker uses a different seed.

![Clipboard Image (6)](https://github.com/user-attachments/assets/9598c94c-d9b4-4ccf-ab16-a21398220aeb)

> [Download workflow](/workflows/distributed-txt2img.json)

1. Open your ComfyUI workflow
2. Add **Distributed Seed** → connect to sampler's seed
3. Add **Distributed Collector** → after VAE Decode
4. Enable workers in the UI
5. Run the workflow!

### Parallel WAN Generation
Generate multiple videos in the time it takes to generate one. Each worker uses a different seed.

![Clipboard Image (5)](https://github.com/user-attachments/assets/5382b845-833b-43b7-b238-a91c5579581a)

> [Download workflow](/workflows/distributed-wan.json)

1. Open your WAN ComfyUI workflow
2. Add **Distributed Seed** → connect to sampler's seed
3. Add **Distributed Collector** → after VAE Decode
4. Add **Image Batch Divider** → after Distributed Collector
5. Set the `divide_by` to the number of GPUs you have available
> For example: if you have a master and 2x workers, set it to 3
7. Enable workers in the UI
8. Run the workflow!

### Distributed Upscaling
Accelerate Ultimate SD Upscaler by distributing tiles across multiple workers, with speed scaling as you add more GPUs.

![Clipboard Image (3)](https://github.com/user-attachments/assets/ffb57a0d-7b75-4497-96d2-875d60865a1a)

> [Download workflow](/workflows/distributed-txt2img.json)

1. Load your image
2. Upscale with ESRGAN or similar
3. Connect to **Ultimate SD Upscale Distributed**
4. Configure tile settings
> If your GPUs are similar, set `static_distribution` to true; otherwise, false
5. Enable workers for faster processing

---

## FAQ

<details>
<summary>Does it combine VRAM of multiple GPUs?</summary>
No, it does not combine VRAM of multiple GPUs.
</details>

<details>
<summary>Does it speed up the generation of a single image or video?</summary>
No, it does not speed up the generation of a single image or video. Instead, it enables the generation of more images or videos simultaneously. However, it can speed up the upscaling of a single image when using the Ultimate SD Upscale Distributed feature.
</details>

<details>
<summary>Does it work with the ComfyUI desktop app?</summary>
Currently, it is not compatible with the ComfyUI desktop app.
</details>

<details>
<summary>Can I combine my RTX 5090 with a GTX 980 to get faster results?</summary>
Yes, you can combine different GPUs, but performance is optimized when using similar GPUs. A significant performance imbalance between GPUs may cause bottlenecks. For upscaling, setting `static_distribution` to `false` allows the faster GPU to handle more processing, which can mitigate some bottlenecks. Note that this setting only applies to upscaling tasks.
</details>

<details>
<summary>Does this work with cloud providers?</summary>
Yes, it is compatible with cloud providers. Refer to the setup guides for detailed instructions.
</details>

<details>
<summary>Can I make this work with my Docker setup?</summary>
Yes, it is compatible with Docker setups, but you will need to configure your Docker environment yourself. Unfortunately, assistance with Docker configuration is not provided.
</details>

---

## Disclaimer

This software is provided "as is" without any warranties, express or implied, including merchantability, fitness for a particular purpose, or non-infringement. The developers and copyright holders are not liable for any claims, damages, or liabilities arising from the use, modification, or distribution of the software. Users are solely responsible for ensuring compliance with applicable laws and regulations and for securing their networks against unauthorized access, hacking, data breaches, or loss. The developers assume no liability for any damages or incidents resulting from misuse, improper configuration, or external threats.

---

## Support the Project

<img width="200"  src="https://github.com/user-attachments/assets/84291921-c44e-4556-94f2-a3b16500f4f9" />

If my custom nodes have added value to your workflow, consider fueling future development with a coffee! Your support helps keep this project thriving. Buy me a coffee at: https://buymeacoffee.com/robertvoy


