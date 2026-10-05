from django.db import migrations, models\nimport django.core.validators
import cloudinary.models


class Migration(migrations.Migration):
    dependencies = [
        ("home", "0025_productmedia"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="media_ai_status",
            field=models.CharField(
                choices=[
                    ("likely_real", "Likely real"),
                    ("needs_review", "Needs review"),
                    ("likely_ai", "Likely AI-generated"),
                ],
                default="needs_review",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="media_ai_score",
            field=models.PositiveSmallIntegerField(default=50),
        ),
        migrations.AddField(
            model_name="product",
            name="media_ai_notes",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name="productmedia",
            name="ai_status",
            field=models.CharField(
                choices=[
                    ("likely_real", "Likely real"),
                    ("needs_review", "Needs review"),
                    ("likely_ai", "Likely AI-generated"),
                ],
                default="needs_review",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="productmedia",
            name="ai_score",
            field=models.PositiveSmallIntegerField(default=50),
        ),
        migrations.AddField(
            model_name="productmedia",
            name="ai_notes",
            field=models.CharField(blank=True, max_length=500),
        ),
        migrations.CreateModel(
            name="ProductVideo",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "video",
                    cloudinary.models.CloudinaryField(
                        folder="shopiva/product-videos",
                        max_length=255,
                        resource_type="video",
                        verbose_name="video",
                    ),
                ),
                (
                    "position",
                    models.PositiveSmallIntegerField(
                        default=0,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(4),
                        ],
                    ),
                ),
                (
                    "ai_status",
                    models.CharField(
                        choices=[
                            ("likely_real", "Likely real"),
                            ("needs_review", "Needs review"),
                            ("likely_ai", "Likely AI-generated"),
                        ],
                        default="needs_review",
                        max_length=24,
                    ),
                ),
                ("ai_score", models.PositiveSmallIntegerField(default=50)),
                ("ai_notes", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="videos",
                        to="home.product",
                    ),
                ),
            ],
            options={
                "ordering": ("position", "created_at"),
                "constraints": [
                    models.UniqueConstraint(
                        fields=("product", "position"),
                        name="unique_product_video_position",
                    )
                ],
            },
        ),
    ]
