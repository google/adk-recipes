# VTO Results for GenMedia TV

This directory contains the VTO (Virtual Try-On) result images displayed in the GenMedia TV "Clothes VTO" channel.

## Directory Structure

```
results/
├── women/
│   ├── result_001.png
│   ├── result_002.png
│   └── ...
└── men/
    ├── result_001.png
    └── ...
```

## Adding New VTO Results

1. **Generate VTO Images**: Use the Image VTO Clothes feature to generate virtual try-on results
2. **Save Results**: Place the generated images in the appropriate gender folder (women/ or men/)
3. **Update Templates**: Edit `/public/products/clothes/image-vto-templates.json` to add entries for the new results

## Template Format

Each entry in the template file should include:
- `model_image`: Path to the original model image
- `garment_images`: Array of garment image paths
- `vto_result`: Path to the generated VTO result (stored here)
- `description`: Description of the virtual try-on
- `scenario`: Description of the scene/background

## Example

```json
{
  "model_image": "/products/clothes/models/women/1_f1.webp",
  "garment_images": [
    "/products/clothes/garments/women/unnamed (1).webp"
  ],
  "vto_result": "/products/clothes/results/women/result_001.png",
  "description": "Elegant white blouse with classic fit",
  "scenario": "Clean white studio background"
}
```

## Image Requirements

- **Format**: PNG or WebP recommended for best quality
- **Size**: Optimal dimensions match the model image size (typically 512x768 or similar)
- **Quality**: High quality to ensure good display in GenMedia TV

## Notes

- Results are automatically displayed in the GenMedia TV interface
- The channel alternates between showing input images (model + garments) and the VTO result
- Images are displayed with smooth transitions and animations
