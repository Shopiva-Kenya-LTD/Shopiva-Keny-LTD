from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from cloudinary.models import CloudinaryField


class ProductMedia(models.Model):
    """Additional real seller-supplied product photos for Shopiva showcases."""

    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="media",
    )
    image = CloudinaryField(
        "image",
        folder="shopiva/product-media",
    )
    position = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(7)],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "position"),
                name="unique_product_media_position",
            ),
        ]

    def __str__(self):
        return f"{self.product.name} media #{self.position + 1}"


class ProductVideo(models.Model):
    """Seller-supplied product video shown in the customer product showcase."""

    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="videos",
    )
    video = CloudinaryField(
        "video",
        folder="shopiva/product-videos",
        resource_type="video",
    )
    position = models.PositiveSmallIntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(4)],
    )
    ai_status = models.CharField(
        max_length=24,
        default="needs_review",
        choices=(
            ("likely_real", "Likely real"),
            ("needs_review", "Needs review"),
            ("likely_ai", "Likely AI-generated"),
        ),
    )
    ai_score = models.PositiveSmallIntegerField(default=50)
    ai_notes = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("position", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("product", "position"),
                name="unique_product_video_position",
            ),
        ]

    def __str__(self):
        return f"{self.product.name} video #{self.position + 1}"
