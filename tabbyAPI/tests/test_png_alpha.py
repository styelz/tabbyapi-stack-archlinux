import io
import unittest

from PIL import Image

from common.png_alpha import apply_requested_alpha


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class PngAlphaTests(unittest.TestCase):
    def test_alpha_punch_is_disabled(self):
        im = Image.new("RGB", (64, 64), (255, 0, 255))
        for y in range(20, 44):
            for x in range(20, 44):
                im.putpixel((x, y), (200, 40, 40))
        raw = _png(im)
        self.assertIs(apply_requested_alpha(raw, wanted=False), raw)
        self.assertIs(apply_requested_alpha(raw, wanted=True), raw)


class VisionSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_pixel_png_is_upscaled_for_qwen_vl(self):
        import base64
        from common.image_util import _VISION_MIN_EDGE, get_image

        pixel = Image.new("RGB", (1, 1), (200, 10, 10))
        buf = io.BytesIO()
        pixel.save(buf, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        image = await get_image(uri)
        self.assertGreaterEqual(image.size[0], _VISION_MIN_EDGE)
        self.assertGreaterEqual(image.size[1], _VISION_MIN_EDGE)
        self.assertEqual(image.getpixel((0, 0))[:3], (200, 10, 10))
