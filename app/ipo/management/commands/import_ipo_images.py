from decimal import Decimal, InvalidOperation
from pathlib import Path
import time

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils.dateparse import parse_date

from ipo.models import HkIpoListing
from ipo.services import IpoImageRecognitionError, recognize_ipo_listing_from_image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class Command(BaseCommand):
    help = "Recognize IPO listing images and import them into HkIpoListing."

    def add_arguments(self, parser):
        parser.add_argument("image_dir", help="Directory containing IPO listing images.")
        parser.add_argument("--dry-run", action="store_true", help="Recognize images without saving records.")
        parser.add_argument("--limit", type=int, default=None, help="Only process the first N images.")
        parser.add_argument("--sleep", type=int, default=0, help="Seconds to sleep after each image.")
        parser.add_argument("--retries", type=int, default=0, help="Retry count for each failed image.")
        parser.add_argument("--retry-sleep", type=int, default=60, help="Seconds to sleep before a retry.")

    def handle(self, *args, **options):
        image_dir = Path(options["image_dir"])
        if not image_dir.exists():
            raise SystemExit(f"Image directory does not exist: {image_dir}")

        image_paths = sorted(
            [path for path in image_dir.iterdir() if path.suffix.lower() in IMAGE_EXTENSIONS],
            key=lambda path: natural_sort_key(path.name),
        )
        if options["limit"]:
            image_paths = image_paths[: options["limit"]]

        total = len(image_paths)
        created = 0
        updated = 0
        failed = 0
        skipped = 0
        self.stdout.write(f"Found {total} images in {image_dir}")

        for index, image_path in enumerate(image_paths, start=1):
            self.stdout.write(f"[{index}/{total}] Recognizing {image_path.name} ...", ending="")
            try:
                fields = recognize_image_file_with_retries(
                    image_path,
                    retries=options["retries"],
                    retry_sleep=options["retry_sleep"],
                    stdout=self.stdout,
                    style=self.style,
                )
            except IpoImageRecognitionError as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f" failed: {exc}"))
                sleep_after_image(options["sleep"])
                continue

            stock_code = normalize_stock_code(fields.get("stock_code"))
            if not stock_code:
                skipped += 1
                self.stdout.write(self.style.WARNING(f" skipped: no stock_code recognized; fields={fields}"))
                sleep_after_image(options["sleep"])
                continue

            fields["stock_code"] = stock_code
            fields = clean_listing_fields(fields)
            if not fields.get("company_name"):
                fields["company_name"] = fields.get("stock_name") or stock_code

            if options["dry_run"]:
                self.stdout.write(
                    self.style.SUCCESS(
                        f" dry-run: {stock_code} {fields.get('stock_name') or fields.get('company_name')}"
                    )
                )
                sleep_after_image(options["sleep"])
                continue

            with transaction.atomic():
                listing = HkIpoListing.objects.filter(stock_code=stock_code).first()
                was_created = listing is None
                if was_created:
                    listing = HkIpoListing(stock_code=stock_code)

                for field_name, value in fields.items():
                    setattr(listing, field_name, value)

                extra_data = listing.extra_data or {}
                imported_images = extra_data.get("imported_images") or []
                if image_path.name not in imported_images:
                    imported_images.append(image_path.name)
                extra_data["imported_images"] = imported_images
                extra_data["last_imported_image"] = image_path.name
                listing.extra_data = extra_data
                listing.save()

            if was_created:
                created += 1
                action = "created"
            else:
                updated += 1
                action = "updated"
            self.stdout.write(self.style.SUCCESS(f" {action}: {stock_code} {listing.stock_name or listing.company_name}"))
            sleep_after_image(options["sleep"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. total={total}, created={created}, updated={updated}, skipped={skipped}, failed={failed}"
            )
        )


def recognize_image_file(image_path):
    content_type = content_type_for(image_path)
    uploaded = SimpleUploadedFile(image_path.name, image_path.read_bytes(), content_type=content_type)
    return recognize_ipo_listing_from_image(uploaded)


def recognize_image_file_with_retries(image_path, retries, retry_sleep, stdout, style):
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return recognize_image_file(image_path)
        except IpoImageRecognitionError as exc:
            if attempt >= attempts:
                raise
            stdout.write(style.WARNING(f" retry {attempt}/{retries} after error: {exc}"))
            time.sleep(retry_sleep)


def sleep_after_image(seconds):
    if seconds:
        time.sleep(seconds)


def content_type_for(path):
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def normalize_stock_code(value):
    if not value:
        return ""
    value = str(value).strip().upper().replace(" ", "")
    if value.isdigit() and len(value) == 5:
        return f"{value}.HK"
    return value


def clean_listing_fields(fields):
    model_fields = {field.name for field in HkIpoListing._meta.fields}
    ignored_fields = {"id", "created_at", "updated_at", "subscription_status", "extra_data"}
    cleaned = {}
    for field_name, value in fields.items():
        if field_name not in model_fields or field_name in ignored_fields:
            continue
        if value in (None, ""):
            continue
        cleaned[field_name] = clean_field_value(HkIpoListing._meta.get_field(field_name), value)
    return cleaned


def clean_field_value(field, value):
    if isinstance(field, models.DateField):
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            return value
        parsed = parse_date(str(value).strip())
        return parsed or value
    if isinstance(field, (models.IntegerField, models.PositiveIntegerField, models.PositiveSmallIntegerField)):
        return int(Decimal(str(value).replace(",", "")))
    if isinstance(field, models.DecimalField):
        try:
            return Decimal(str(value).replace(",", "").replace("%", "").strip())
        except (InvalidOperation, ValueError):
            return value
    if isinstance(field, models.BooleanField):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "y"}
    return value


def natural_sort_key(value):
    parts = []
    current = ""
    is_digit = None
    for char in value:
        char_is_digit = char.isdigit()
        if is_digit is None or char_is_digit == is_digit:
            current += char
        else:
            parts.append(int(current) if is_digit else current.lower())
            current = char
        is_digit = char_is_digit
    if current:
        parts.append(int(current) if is_digit else current.lower())
    return parts
