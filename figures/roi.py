import sys
from PIL import Image, ImageDraw, ImageFont
src, out = sys.argv[1], sys.argv[2]
roi = (0.234375, 0.3, 0.4765625, 0.95)
im = Image.open(src).convert('RGB')
w, h = im.size
box = tuple(round(v*(w if i % 2 == 0 else h)) for i, v in enumerate(roi))
d = ImageDraw.Draw(im)
for k in range(3):
    d.rectangle((box[0]-k, box[1]-k, box[2]+k, box[3]+k), outline=(56, 224, 90))
font = ImageFont.truetype('/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc', 16)
d.rectangle((box[0], box[1]-24, box[0]+46, box[1]-2), fill=(56, 224, 90))
d.text((box[0]+6, box[1]-23), 'ROI', fill=(0, 0, 0), font=font)
im.save(out, quality=92)
print(im.size, box)
