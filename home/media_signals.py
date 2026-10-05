from django.db.models.signals import post_save
from django.dispatch import receiver

from .media_pipeline import enhance_product_image
from .media_authenticity import screen_image
from .models import Product
from .models_product_media import ProductMedia


@receiver(post_save, sender=Product)
def persist_product_gallery(sender, instance, **kwargs):
    """Turn selected real seller photos into stored, presentation-ready gallery media."""
    files = getattr(instance, "_shopiva_gallery_files", None) or []
    if not files:
        return

    try:
        last_position = (
            ProductMedia.objects.filter(product=instance)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        )
        position = (last_position + 1) if last_position is not None else 0

        for uploaded in files:
            if position > 7:
                break
            try:
                enhanced = enhance_product_image(uploaded, instance.name)
                review = screen_image(uploaded)
                media = ProductMedia(
                    product=instance,
                    image=enhanced,
                    position=position,
                    ai_status=review["status"],
                    ai_score=review["score"],
                    ai_notes=review["notes"],
                )
                media.save()
                position += 1
            except Exception:
                # One bad optional gallery image must never block the product listing.
                continue
    finally:
        try:
            delattr(instance, "_shopiva_gallery_files")
        except AttributeError:
            pass
