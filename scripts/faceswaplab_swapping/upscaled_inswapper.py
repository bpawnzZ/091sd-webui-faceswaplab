from typing import Any, Optional, Tuple, Union
import cv2
import numpy as np
from insightface.model_zoo.inswapper import INSwapper
from insightface.utils import face_align
from modules import processing, shared
from modules.upscaler import UpscalerData

from scripts.faceswaplab_postprocessing import upscaling
from scripts.faceswaplab_postprocessing.postprocessing_options import (
    PostProcessingOptions,
)
from scripts.faceswaplab_swapping.facemask import generate_face_mask
from scripts.faceswaplab_swapping.upcaled_inswapper_options import InswappperOptions
from scripts.faceswaplab_utils.imgutils import cv2_to_pil, pil_to_cv2
from scripts.faceswaplab_utils.sd_utils import get_sd_option
from scripts.faceswaplab_utils.typing import CV2ImgU8, Face
from scripts.faceswaplab_utils.faceswaplab_logging import logger


def get_upscaler() -> Optional[UpscalerData]:
    for upscaler in shared.sd_upscalers:
        if upscaler.name == get_sd_option(
            "faceswaplab_upscaled_swapper_upscaler", "LDSR"
        ):
            return upscaler
    return None


def merge_images_with_mask(
    image1: CV2ImgU8, image2: CV2ImgU8, mask: CV2ImgU8
) -> CV2ImgU8:
    """
    Merges two images using a given mask. The regions where the mask is set will be replaced with the corresponding
    areas of the second image.

    Args:
        image1 (CV2Img): The base image, which must have the same shape as image2.
        image2 (CV2Img): The image to be merged, which must have the same shape as image1.
        mask (CV2Img): A binary mask specifying the regions to be merged. The mask shape should match image1's first two dimensions.

    Returns:
        CV2Img: The merged image.

    Raises:
        ValueError: If the shapes of the images and mask do not match.
    """

    if image1.shape != image2.shape or image1.shape[:2] != mask.shape:
        raise ValueError("Img should have the same shape")
    mask = mask.astype(np.uint8)
    masked_region = cv2.bitwise_and(image2, image2, mask=mask)
    inverse_mask = cv2.bitwise_not(mask)
    empty_region = cv2.bitwise_and(image1, image1, mask=inverse_mask)
    merged_image = cv2.add(empty_region, masked_region)
    return merged_image


def erode_mask(mask: CV2ImgU8, kernel_size: int = 3, iterations: int = 1) -> CV2ImgU8:
    """
    Erodes a binary mask using a given kernel size and number of iterations.

    Args:
        mask (CV2Img): The binary mask to erode.
        kernel_size (int, optional): The size of the kernel. Default is 3.
        iterations (int, optional): The number of erosion iterations. Default is 1.

    Returns:
        CV2Img: The eroded mask.
    """
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    eroded_mask = cv2.erode(mask, kernel, iterations=iterations)
    return eroded_mask


def apply_gaussian_blur(
    mask: CV2ImgU8, kernel_size: Tuple[int, int] = (5, 5), sigma_x: int = 0
) -> CV2ImgU8:
    """
    Applies a Gaussian blur to a mask.

    Args:
        mask (CV2Img): The mask to blur.
        kernel_size (tuple, optional): The size of the kernel, e.g. (5, 5). Default is (5, 5).
        sigma_x (int, optional): The standard deviation in the X direction. Default is 0.

    Returns:
        CV2Img: The blurred mask.
    """
    blurred_mask = cv2.GaussianBlur(mask, kernel_size, sigma_x)
    return blurred_mask


def dilate_mask(mask: CV2ImgU8, kernel_size: int = 5, iterations: int = 1) -> CV2ImgU8:
    """
    Dilates a binary mask using a given kernel size and number of iterations.

    Args:
        mask (CV2Img): The binary mask to dilate.
        kernel_size (int, optional): The size of the kernel. Default is 5.
        iterations (int, optional): The number of dilation iterations. Default is 1.

    Returns:
        CV2Img: The dilated mask.
    """
    kernel = np.ones((kernel_size, kernel_size), np.uint8)
    dilated_mask = cv2.dilate(mask, kernel, iterations=iterations)
    return dilated_mask


def get_face_mask(aimg: CV2ImgU8, bgr_fake: CV2ImgU8) -> CV2ImgU8:
    """
    Generates a face mask by performing bitwise OR on two face masks and then dilating the result.

    Args:
        aimg (CV2Img): Input image for generating the first face mask.
        bgr_fake (CV2Img): Input image for generating the second face mask.

    Returns:
        CV2Img: The combined and dilated face mask.
    """
    mask1 = generate_face_mask(aimg, device=shared.device)
    mask2 = generate_face_mask(bgr_fake, device=shared.device)
    mask = dilate_mask(cv2.bitwise_or(mask1, mask2))
    return mask


class UpscaledINSwapper(INSwapper):
    def __init__(self, inswapper: INSwapper):
        self.__dict__.update(inswapper.__dict__)

    def upscale_and_restore(
        self,
        img: CV2ImgU8,
        k: int = 2,
        inswapper_options: Optional[InswappperOptions] = None,
    ) -> CV2ImgU8:
        if inswapper_options is None:
            return img

        pil_img = cv2_to_pil(img)
        pp_options = PostProcessingOptions(
            upscaler_name=inswapper_options.upscaler_name,
            upscale_visibility=1,
            scale=k,
            face_restorer_name=inswapper_options.face_restorer_name,
            codeformer_weight=inswapper_options.codeformer_weight,
            restorer_visibility=inswapper_options.restorer_visibility,
        )

        upscaled = pil_img
        if pp_options.upscaler_name:
            upscaled = upscaling.upscale_img(pil_img, pp_options)
        if pp_options.face_restorer_name:
            upscaled = upscaling.restore_face(upscaled, pp_options)

        return pil_to_cv2(upscaled)

    def get(
        self,
        img: CV2ImgU8,
        target_face: Face,
        source_face: Face,
        paste_back: bool = True,
        options: Optional[InswappperOptions] = None,
    ) -> Union[CV2ImgU8, Tuple[CV2ImgU8, Any]]:
        aimg, M = face_align.norm_crop2(img, target_face.kps, self.input_size[0])
        blob = cv2.dnn.blobFromImage(
            aimg,
            1.0 / self.input_std,
            self.input_size,
            (self.input_mean, self.input_mean, self.input_mean),
            swapRB=True,
        )
        latent = source_face.normed_embedding.reshape((1, -1))  # type: ignore
        latent = np.dot(latent, self.emap)
        latent /= np.linalg.norm(latent)
        assert self.session is not None
        pred = self.session.run(
            self.output_names, {self.input_names[0]: blob, self.input_names[1]: latent}
        )[0]
        # print(latent.shape, latent.dtype, pred.shape)
        img_fake = pred.transpose((0, 2, 3, 1))[0]
        bgr_fake = np.clip(255 * img_fake, 0, 255).astype(np.uint8)[:, :, ::-1]

        try:
            if not paste_back:
                return bgr_fake, M
            else:
                target_img = img

                def compute_diff(bgr_fake: CV2ImgU8, aimg: CV2ImgU8) -> CV2ImgU8:
                    fake_diff = bgr_fake.astype(np.float32) - aimg.astype(np.float32)
                    fake_diff = np.abs(fake_diff).mean(axis=2)
                    fake_diff[:2, :] = 0
                    fake_diff[-2:, :] = 0
                    fake_diff[:, :2] = 0
                    fake_diff[:, -2:] = 0
                    return fake_diff

                if options:
                    logger.info("*" * 80)
                    logger.info(f"Inswapper")

                    if options.upscaler_name and options.upscaler_name != "None":
                        # Upscale original image
                        k = 4
                        aimg, M = face_align.norm_crop2(
                            img, target_face.kps, self.input_size[0] * k
                        )
                    else:
                        k = 1

                    # upscale and restore face :
                    bgr_fake = self.upscale_and_restore(
                        bgr_fake, inswapper_options=options, k=k
                    )

                    fake_diff: CV2ImgU8 = None  # type: ignore

                    if not options.improved_mask:
                        # If improved mask is not used, we should compute before sharpen and color correction (better diff)
                        fake_diff = compute_diff(bgr_fake, aimg=aimg)

                    if options.sharpen:
                        logger.info("sharpen")
                        # Add sharpness
                        blurred = cv2.GaussianBlur(bgr_fake, (0, 0), 3)
                        bgr_fake = cv2.addWeighted(bgr_fake, 1.5, blurred, -0.5, 0)

                    # Apply color corrections
                    if options.color_corrections:
                        logger.info("color correction")
                        correction = processing.setup_color_correction(cv2_to_pil(aimg))
                        bgr_fake_pil = processing.apply_color_correction(
                            correction, cv2_to_pil(bgr_fake)
                        )
                        bgr_fake = pil_to_cv2(bgr_fake_pil)

                    if options.improved_mask:
                        if k == 1:
                            logger.warning(
                                "Please note that improved mask does not work well without upscaling. Set upscaling to Lanczos at least if you want speed and want to use improved mask."
                            )

                        logger.info("improved_mask")
                        mask = get_face_mask(aimg, bgr_fake)
                        # save_img_debug(cv2_to_pil(bgr_fake), "Before Mask")
                        bgr_fake = merge_images_with_mask(aimg, bgr_fake, mask)
                        # save_img_debug(cv2_to_pil(bgr_fake), "After Mask")

                        fake_diff = compute_diff(bgr_fake, aimg=aimg)

                    assert (
                        fake_diff is not None
                    ), "fake diff is None, this should not happen"

                    logger.info("*" * 80)

                else:
                    fake_diff = compute_diff(bgr_fake, aimg)

                IM = cv2.invertAffineTransform(M)

                img_white = np.full(
                    (aimg.shape[0], aimg.shape[1]), 255, dtype=np.float32
                )
                bgr_fake = cv2.warpAffine(
                    bgr_fake,
                    IM,
                    (target_img.shape[1], target_img.shape[0]),
                    borderValue=0.0,
                )
                img_white = cv2.warpAffine(
                    img_white,
                    IM,
                    (target_img.shape[1], target_img.shape[0]),
                    borderValue=0.0,
                )

                fake_diff = cv2.warpAffine(
                    fake_diff,
                    IM,
                    (target_img.shape[1], target_img.shape[0]),
                    borderValue=0.0,
                )
                img_white[img_white > 20] = 255
                fthresh = 10
                fake_diff[fake_diff < fthresh] = 0
                fake_diff[fake_diff >= fthresh] = 255
                img_mask = img_white
                mask_h_inds, mask_w_inds = np.where(img_mask == 255)
                mask_h = np.max(mask_h_inds) - np.min(mask_h_inds)
                mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
                mask_size = int(np.sqrt(mask_h * mask_w))
                erosion_factor = options.erosion_factor if options else 1

                k = max(int(mask_size // 10 * erosion_factor), int(10 * erosion_factor))

                kernel = np.ones((k, k), np.uint8)
                img_mask = cv2.erode(img_mask, kernel, iterations=1)
                kernel = np.ones((2, 2), np.uint8)
                fake_diff = cv2.dilate(fake_diff, kernel, iterations=1)
                k = max(int(mask_size // 20 * erosion_factor), int(5 * erosion_factor))

                kernel_size = (k, k)
                blur_size = tuple(2 * i + 1 for i in kernel_size)
                img_mask = cv2.GaussianBlur(img_mask, blur_size, 0)
                k = int(5 * erosion_factor)
                kernel_size = (k, k)
                blur_size = tuple(2 * i + 1 for i in kernel_size)
                fake_diff = cv2.GaussianBlur(fake_diff, blur_size, 0)
                img_mask /= 255
                fake_diff /= 255

                img_mask = np.reshape(
                    img_mask, [img_mask.shape[0], img_mask.shape[1], 1]
                )
                fake_merged = img_mask * bgr_fake + (1 - img_mask) * target_img.astype(
                    np.float32
                )
                fake_merged = fake_merged.astype(np.uint8)
                return fake_merged
        except Exception as e:
            import traceback

            traceback.print_exc()
            raise e
