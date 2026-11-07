"""SSIMULACRA2 perceptual image metric (vendored from the BSD-licensed `ssimulacra2` package,
with its single SciPy call replaced by an equivalent NumPy Gaussian filter).

BSD 3-Clause License

Copyright (c) 2025, Pacidus
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
"""SSIMULACRA2 implementation in Python."""

import numpy as np
from PIL import Image

# Constants from the original implementation
kC2 = 0.0009
kNumScales = 6

# Parameters for opsin absorbance - exact same values as in C++
# From src/lib/jxl/opsin_params.h
kM02 = 0.078
kM00 = 0.30
kM01 = 1.0 - kM02 - kM00

kM12 = 0.078
kM10 = 0.23
kM11 = 1.0 - kM12 - kM10

kM20 = 0.24342268924547819
kM21 = 0.20476744424496821
kM22 = 1.0 - kM20 - kM21

kB0 = 0.0037930732552754493
kB1 = kB0
kB2 = kB0

# Create the matrix and bias arrays exactly as in C++
kOpsinAbsorbanceMatrix = np.array(
    [kM00, kM01, kM02, kM10, kM11, kM12, kM20, kM21, kM22]
)

kOpsinAbsorbanceBias = np.array([kB0, kB1, kB2])
weights = np.array(
    [
        0.0,
        0.0007376606707406586,
        0.0,
        0.0,
        0.0007793481682867309,
        0.0,
        0.0,
        0.0004371155730107379,
        0.0,
        1.1041726426657346,
        0.00066284834129271,
        0.00015231632783718752,
        0.0,
        0.0016406437456599754,
        0.0,
        1.8422455520539298,
        11.441172603757666,
        0.0,
        0.0007989109436015163,
        0.000176816438078653,
        0.0,
        1.8787594979546387,
        10.94906990605142,
        0.0,
        0.0007289346991508072,
        0.9677937080626833,
        0.0,
        0.00014003424285435884,
        0.9981766977854967,
        0.00031949755934435053,
        0.0004550992113792063,
        0.0,
        0.0,
        0.0013648766163243398,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        7.466890328078848,
        0.0,
        17.445833984131262,
        0.0006235601634041466,
        0.0,
        0.0,
        6.683678146179332,
        0.00037724407979611296,
        1.027889937768264,
        225.20515300849274,
        0.0,
        0.0,
        19.213238186143016,
        0.0011401524586618361,
        0.001237755635509985,
        176.39317598450694,
        0.0,
        0.0,
        24.43300999870476,
        0.28520802612117757,
        0.0004485436923833408,
        0.0,
        0.0,
        0.0,
        34.77906344483772,
        44.835625328877896,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0008680556573291698,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0005313191874358747,
        0.0,
        0.00016533814161379112,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0004179171803251336,
        0.0017290828234722833,
        0.0,
        0.0020827005846636437,
        0.0,
        0.0,
        8.826982764996862,
        23.19243343998926,
        0.0,
        95.1080498811086,
        0.9863978034400682,
        0.9834382792465353,
        0.0012286405048278493,
        171.2667255897307,
        0.9807858872435379,
        0.0,
        0.0,
        0.0,
        0.0005130064588990679,
        0.0,
        0.00010854057858411537,
    ],
    dtype=np.float64,
)


def srgb_to_linear(img):
    """Convert sRGB to linear RGB."""
    # Handle the two-part curve
    rgb = np.asarray(img, dtype=np.float64) / 255.0
    mask = rgb <= 0.04045
    rgb[mask] = rgb[mask] / 12.92
    rgb[~mask] = ((rgb[~mask] + 0.055) / 1.055) ** 2.4
    return rgb


def linear_rgb_to_xyb(linear):
    """Convert linear RGB to XYB color space."""
    # Apply opsin absorbance (matrix multiplication)
    r, g, b = linear.T

    # Reshape the matrix for easier operations
    matrix = kOpsinAbsorbanceMatrix.reshape(3, 3)

    # Apply matrix multiplication
    mixed0 = (
        matrix[0, 0] * r + matrix[0, 1] * g + matrix[0, 2] * b + kOpsinAbsorbanceBias[0]
    )
    mixed1 = (
        matrix[1, 0] * r + matrix[1, 1] * g + matrix[1, 2] * b + kOpsinAbsorbanceBias[1]
    )
    mixed2 = (
        matrix[2, 0] * r + matrix[2, 1] * g + matrix[2, 2] * b + kOpsinAbsorbanceBias[2]
    )

    # Zero if negative
    mixed0 = np.maximum(mixed0, 0)
    mixed1 = np.maximum(mixed1, 0)
    mixed2 = np.maximum(mixed2, 0)

    # Apply cube root and subtract bias
    mixed0 = np.cbrt(mixed0) - np.cbrt(kOpsinAbsorbanceBias[0])
    mixed1 = np.cbrt(mixed1) - np.cbrt(kOpsinAbsorbanceBias[1])
    mixed2 = np.cbrt(mixed2) - np.cbrt(kOpsinAbsorbanceBias[2])

    # Store in XYB format
    x = 0.5 * (mixed0 - mixed1)
    y = 0.5 * (mixed0 + mixed1)
    b_y = mixed2

    return np.stack([x, y, b_y], axis=2)


def make_positive_xyb(xyb):
    """Adjust XYB values to positive range as in SSIMULACRA2."""
    result = xyb.copy()

    # MakePositiveXYB function from SSIMULACRA2
    result[:, :, 2] = (result[:, :, 2] - result[:, :, 1]) + 0.55  # B-Y
    result[:, :, 0] = result[:, :, 0] * 14.0 + 0.42  # X scaling
    result[:, :, 1] += 0.01  # Y offset

    return result


def downsample(img, fx, fy):
    """Downsample image using reshape for the main part and classic method for edges."""
    h, w, c = img.shape
    out_h, out_w = (h + fy - 1) // fy, (w + fx - 1) // fx

    # Calculate dimensions for the clean part (divisible by factors)
    clean_h = (h // fy) * fy
    clean_w = (w // fx) * fx
    clean_out_h = clean_h // fy
    clean_out_w = clean_w // fx

    # Initialize result array
    result = np.zeros((out_h, out_w, c), dtype=np.float64)

    # Process main part with reshape method
    if clean_h > 0 and clean_w > 0:
        # Use reshape for the cleanly divisible portion
        result[:clean_out_h, :clean_out_w, :] = (
            img[:clean_h, :clean_w, :]
            .reshape(clean_out_h, fy, clean_out_w, fx, c)
            .mean(axis=(1, 3))
        )

    # Handle right edge (except corner)
    if clean_w < w:
        for oy in range(clean_out_h):
            y_start = oy * fy
            y_end = (oy + 1) * fy
            x_start = clean_w
            x_end = w
            result[oy, clean_out_w:, :] = img[y_start:y_end, x_start:x_end, :].mean(
                axis=(0, 1)
            )

    # Handle bottom edge (except corner)
    if clean_h < h:
        for ox in range(clean_out_w):
            y_start = clean_h
            y_end = h
            x_start = ox * fx
            x_end = (ox + 1) * fx
            result[clean_out_h:, ox, :] = img[y_start:y_end, x_start:x_end, :].mean(
                axis=(0, 1)
            )

    # Handle bottom-right corner if needed
    if clean_h < h and clean_w < w:
        result[clean_out_h:, clean_out_w:, :] = img[clean_h:, clean_w:, :].mean(
            axis=(0, 1)
        )

    return result


def _gaussian_reflect(image, sigma, truncate):
    """NumPy equivalent of scipy.ndimage.gaussian_filter(mode="reflect") over axes 0 and 1."""
    r = int(truncate * sigma + 0.5)
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    out = image
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="symmetric")
        n = out.shape[axis]
        acc = None
        for i, w in enumerate(k):
            sl = [slice(None)] * out.ndim
            sl[axis] = slice(i, i + n)
            term = p[tuple(sl)] * w
            acc = term if acc is None else acc + term
        out = acc
    return out

def blur_image(image, sigma=1.5):
    """Apply Gaussian blur to an image.

    This uses scipy's Gaussian filter with mode='mirror' and padding to
    match the behavior in the C++ implementation.
    """
    # Calculate radius using the same formula as in the C++ code
    radius = int(round(3.2795 * sigma + 0.2546))

    # Get original image dimensions
    height, width, c = image.shape

    # Only pad vertically with zeros (top and bottom)
    padded = np.zeros((height + 2 * radius, width, c), dtype=np.float64)
    # Copy original image to middle section
    padded[radius : radius + height, :, :] = image

    # Apply gaussian filter with 'reflect' mode
    # This will handle horizontal boundaries automatically using reflection
    filtered = _gaussian_reflect(padded, sigma, truncate=3.33)

    # Extract the result (removing vertical padding)
    return filtered[radius : radius + height, :]


def ssim_map(m1, m2, s11, s22, s12):
    """Compute the SSIM map between two images."""
    plane_averages = np.zeros(6)

    # For each channel

    m11 = m1 * m1
    m22 = m2 * m2
    m12 = m1 * m2

    num_m = 1.0 - (m1 - m2) * (m1 - m2)
    num_s = 2.0 * (s12 - m12) + kC2
    denom_s = (s11 - m11) + (s22 - m22) + kC2

    d = 1.0 - (num_m * num_s / denom_s)
    d = np.maximum(d, 0.0)

    # Store averages
    plane_averages[::2] = d.mean((0, 1))
    L4 = d * d
    L4 = L4 * L4
    plane_averages[1::2] = L4.mean((0, 1)) ** 0.25

    return plane_averages


def edge_diff_map(img1, mu1, img2, mu2):
    """Compute edge difference maps."""
    plane_averages = np.zeros(12)

    # Compute ratio of edge strengths
    d1 = (1.0 + np.abs(img2 - mu2)) / (1.0 + np.abs(img1 - mu1)) - 1.0

    # d1 > 0: distorted has edges where original is smooth (ringing/blockiness)
    artifact = np.maximum(d1, 0.0)
    plane_averages[::4] = artifact.mean((0, 1))
    L4 = artifact * artifact
    L4 = L4 * L4
    plane_averages[1::4] = L4.mean((0, 1)) ** 0.25

    # d1 < 0: original has edges where distorted is smooth (blurring)
    detail_lost = np.maximum(-d1, 0.0)
    plane_averages[2::4] = detail_lost.mean((0, 1))
    L4 = detail_lost * detail_lost
    L4 = L4 * L4
    plane_averages[3::4] = L4.mean((0, 1)) ** 0.25

    return plane_averages


def alpha_blend(img, alpha, bg=0.5):
    """Alpha blend image with background color."""
    if alpha is None:
        return img

    result = img.copy()
    for c in range(3):
        result[:, :, c] = alpha * img[:, :, c] + (1.0 - alpha) * bg

    return result


class MsssimScale:
    """Data structure for one scale's metrics."""

    def __init__(self):
        self.avg_ssim = np.zeros(3 * 2)  # 3 channels, 2 norms
        # 3 channels, 4 values (ringing and blurring, each with 2 norms)
        self.avg_edgediff = np.zeros(12)


class Msssim:
    """Multi-scale structural similarity metric."""

    def __init__(self):
        self.scales = []

    def score(self):
        """Compute the final SSIMULACRA2 score."""
        # These weights were obtained from the original C++ implementation

        ssim = 0.0
        i = 0
        # Apply weights to each component
        for c in range(3):
            for scale in range(len(self.scales)):
                for n in range(2):
                    ssim += weights[i] * abs(self.scales[scale].avg_ssim[c * 2 + n])
                    i += 1
                    ssim += weights[i] * abs(self.scales[scale].avg_edgediff[c * 4 + n])
                    i += 1
                    ssim += weights[i] * abs(
                        self.scales[scale].avg_edgediff[c * 4 + n + 2]
                    )
                    i += 1

        # Transform to final score
        ssim = ssim * 0.9562382616834844
        ssim = (
            2.326765642916932 * ssim
            - 0.020884521182843837 * ssim * ssim
            + 6.248496625763138e-05 * ssim * ssim * ssim
        )

        if ssim > 0:
            ssim = 100.0 - 10.0 * pow(ssim, 0.6276336467831387)
        else:
            ssim = 100.0

        return ssim


def compute_ssimulacra2(orig_path, dist_path, bg=0.5):
    """Compute SSIMULACRA2 score between original and distorted images."""
    # Load images
    orig_img = np.array(Image.open(orig_path).convert("RGB"), dtype=np.float64)
    dist_img = np.array(Image.open(dist_path).convert("RGB"), dtype=np.float64)

    # Check if alpha channel exists (assuming 4-channel means RGBA)
    orig_alpha = None
    dist_alpha = None
    if len(orig_img.shape) > 2 and orig_img.shape[2] == 4:
        orig_alpha = orig_img[:, :, 3] / 255.0
        orig_img = orig_img[:, :, :3]
    if len(dist_img.shape) > 2 and dist_img.shape[2] == 4:
        dist_alpha = dist_img[:, :, 3] / 255.0
        dist_img = dist_img[:, :, :3]

    # Alpha blending if needed
    if orig_alpha is not None:
        orig_img = alpha_blend(orig_img, orig_alpha, bg)
    if dist_alpha is not None:
        dist_img = alpha_blend(dist_img, dist_alpha, bg)

    # Convert sRGB to linear RGB
    orig_linear = srgb_to_linear(orig_img)
    dist_linear = srgb_to_linear(dist_img)

    # Convert to XYB
    orig_xyb = linear_rgb_to_xyb(orig_linear)
    dist_xyb = linear_rgb_to_xyb(dist_linear)

    # Make XYB values positive
    orig_xyb = make_positive_xyb(orig_xyb)
    dist_xyb = make_positive_xyb(dist_xyb)

    # Initialize multi-scale metric
    msssim = Msssim()

    # Process at multiple scales
    img1 = orig_xyb
    img2 = dist_xyb

    for scale in range(kNumScales):
        # Check if image is too small to process
        if img1.shape[0] < 8 or img1.shape[1] < 8:
            break

        # Create multiplied images for variance calculations
        mul = img1 * img1
        sigma1_sq = blur_image(mul)

        mul = img2 * img2
        sigma2_sq = blur_image(mul)

        mul = img1 * img2
        sigma12 = blur_image(mul)

        # Compute blurred means
        mu1 = blur_image(img1)
        mu2 = blur_image(img2)

        # Create scale data structure
        scale_data = MsssimScale()

        # Compute SSIM map
        scale_data.avg_ssim = ssim_map(mu1, mu2, sigma1_sq, sigma2_sq, sigma12)

        # Compute edge difference maps
        scale_data.avg_edgediff = edge_diff_map(img1, mu1, img2, mu2)

        # Add to scales
        msssim.scales.append(scale_data)

        # Downsample for next scale (if not the last scale)
        if scale < kNumScales - 1:
            # Downsample in linear RGB space for better quality
            orig_linear = downsample(orig_linear, 2, 2)
            dist_linear = downsample(dist_linear, 2, 2)

            # Convert back to XYB
            img1 = linear_rgb_to_xyb(orig_linear)
            img2 = linear_rgb_to_xyb(dist_linear)

            # Make XYB values positive
            img1 = make_positive_xyb(img1)
            img2 = make_positive_xyb(img2)

    # Compute final score
    return msssim.score()


def compute_ssimulacra2_with_alpha(orig_path, dist_path):
    """For images with alpha, compute SSIMULACRA2 with dark/light backgrounds."""
    # Test if images have alpha channel
    has_alpha = False

    try:
        img = Image.open(orig_path)
        has_alpha = img.mode == "RGBA"
    except:
        pass

    if not has_alpha:
        return compute_ssimulacra2(orig_path, dist_path)

    # For images with alpha, compute with dark and light backgrounds
    score_dark = compute_ssimulacra2(orig_path, dist_path, bg=0.1)
    score_light = compute_ssimulacra2(orig_path, dist_path, bg=0.9)

    # Return the worse (lower) of the two scores
    return min(score_dark, score_light)
