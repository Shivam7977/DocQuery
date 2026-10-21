"""
Visual content extraction (Phase 4): finds embedded raster images AND
vector-drawn charts/graphs on each page, extracts/renders them, and asks a
vision model to describe them. Each description becomes its own chunk —
element_type="image" for raster figures/photos, element_type="chart" for
vector-drawn charts.

This is intentionally a separate, self-contained module (not merged into
pdf_parser.py or table_extractor.py) — the pipeline stays: TextExtractor,
TableExtractor, and this VisualExtractor each own one concern, and only
VisualExtractor talks to a VisionProvider.
"""
import fitz  # pymupdf
import io
from PIL import Image, ImageChops
from app.services.caption_utils import find_nearby_caption
from app.services.vision_provider import get_vision_provider

FIGURE_CAPTION_PREFIXES = ("figure", "fig.", "chart", "diagram")
CAPTION_MAX_WORDS = 20
IMAGE_DUPLICATE_MAX_HASH_DISTANCE = 6
IMAGE_DUPLICATE_MAX_ASPECT_DELTA = 0.08
MAX_UNCAPTIONED_VECTOR_REGION_WORDS = 40
MIN_UNCAPTIONED_VECTOR_WIDTH = 100
MIN_UNCAPTIONED_VECTOR_HEIGHT = 80
MAX_UNCAPTIONED_VECTOR_ASPECT_RATIO = 4.0

# Raster images below this size are almost always decorative (bullet icons,
# logos, thin divider graphics) rather than meaningful figures — skip them
# so they don't waste a vision-model call on nothing useful.
MIN_IMAGE_DIMENSION_PX = 150

# Vector-drawing clustering: how close two drawing bounding boxes need to be
# (in PDF points) to be treated as parts of the same chart, and how many
# distinct drawing items a cluster needs before it's treated as a real
# chart rather than a simple decorative box/border/underline.
CLUSTER_MERGE_DISTANCE = 15
MIN_DRAWINGS_PER_CHART = 8

# SECURITY / COST: without a cap, a PDF containing an excessive number of
# images/charts would trigger one vision-model API call per visual with no
# limit — a real cost-abuse vector once MISTRAL_API_KEY is live in
# production (each call costs money). Candidates beyond this count are
# simply skipped (kept in page order), so a document still gets useful
# visual-content coverage rather than failing outright.
MAX_VISUALS_PER_DOCUMENT = 20


def _extract_raster_images(page, doc, page_number: int) -> list[dict]:
    """Finds embedded raster images (photos, scanned figures, logos-sized-up, etc.)."""
    results = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            base_image = doc.extract_image(xref)
        except Exception:
            continue

        if base_image["width"] < MIN_IMAGE_DIMENSION_PX or base_image["height"] < MIN_IMAGE_DIMENSION_PX:
            continue  # too small to be a meaningful figure

        rects = page.get_image_rects(xref)
        bbox = tuple(rects[0]) if rects else None

        image_bytes = base_image["image"]
        soft_mask_xref = base_image.get("smask")
        if soft_mask_xref:
            # extract_image() returns the base stream separately from its
            # soft mask. Sending only the base stream can turn transparent
            # PDF graphics into a solid/incorrect image for the vision model.
            base_pixmap = fitz.Pixmap(doc, xref)
            if base_pixmap.alpha:
                base_pixmap = fitz.Pixmap(base_pixmap, 0)
            mask_pixmap = fitz.Pixmap(doc, soft_mask_xref)
            merged_pixmap = fitz.Pixmap(base_pixmap, mask_pixmap)
            image_bytes = merged_pixmap.tobytes("png")
            # Some PDFs contain fully transparent image/mask pairs. They are
            # layout resources, not visible figures, and should not trigger a
            # vision request.
            with Image.open(io.BytesIO(image_bytes)) as masked_image:
                alpha = masked_image.getchannel("A") if masked_image.mode == "RGBA" else None
                if alpha is not None and alpha.getbbox() is None:
                    continue

        results.append({
            "image_bytes": image_bytes,
            "bbox": bbox,
            "page": page_number,
            "kind": "image",
        })

    return results


def _image_fingerprint(image_bytes: bytes) -> tuple[float, tuple[bool, ...]]:
    """Return a small perceptual hash and aspect ratio for duplicate detection."""
    with Image.open(io.BytesIO(image_bytes)) as source:
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        normalized = Image.alpha_composite(background, rgba).convert("L")
        aspect_ratio = normalized.width / max(normalized.height, 1)
        small = normalized.resize((8, 8), Image.Resampling.LANCZOS)
        pixels = list(small.getdata())
    average = sum(pixels) / len(pixels)
    return aspect_ratio, tuple(pixel >= average for pixel in pixels)


def _deduplicate_raster_visuals(candidates: list[dict]) -> tuple[list[int], list[int]]:
    """Return representative indices and an index-to-representative map.

    Repeated logos remain represented on every page; only duplicate vision
    requests are removed. Similarity is intentionally limited to raster
    images with nearly matching aspect ratios.
    """
    representative_indices: list[int] = []
    representatives: list[tuple[float, tuple[bool, ...]]] = []
    index_to_representative: list[int] = []

    for index, candidate in enumerate(candidates):
        if candidate["kind"] != "image":
            representative_indices.append(index)
            representatives.append((0.0, ()))
            index_to_representative.append(index)
            continue

        fingerprint = _image_fingerprint(candidate["image_bytes"])
        duplicate_index = None
        for representative_index, existing in zip(representative_indices, representatives):
            aspect_delta = abs(fingerprint[0] - existing[0]) / max(fingerprint[0], existing[0], 1e-9)
            hash_distance = sum(left != right for left, right in zip(fingerprint[1], existing[1]))
            if (aspect_delta <= IMAGE_DUPLICATE_MAX_ASPECT_DELTA
                    and hash_distance <= IMAGE_DUPLICATE_MAX_HASH_DISTANCE):
                duplicate_index = representative_index
                break

        if duplicate_index is None:
            representative_indices.append(index)
            representatives.append(fingerprint)
            duplicate_index = index
        index_to_representative.append(duplicate_index)

    return representative_indices, index_to_representative


def _cluster_drawings(drawings: list[dict]) -> list[fitz.Rect]:
    """
    Merges nearby/overlapping drawing bounding boxes into clusters. A chart
    is typically made of many small path segments (bars, axis ticks,
    gridlines) scattered close together — clustering turns that scatter
    into one bounding region per chart, and keeps two side-by-side charts
    on the same page as two separate clusters instead of merging them.
    """
    rects = [fitz.Rect(d["rect"]) for d in drawings if d.get("rect")]
    if not rects:
        return []

    clusters: list[list[fitz.Rect]] = [[r] for r in rects]

    # Merge nearby boxes by connected components. The previous implementation
    # recomputed both cluster bounds inside nested loops and restarted the full
    # scan after every merge. Dense pages (hundreds of vector paths) made that
    # work grow explosively before the document-level visual cap was applied.
    # This sweep checks only boxes whose x-ranges could intersect and computes
    # each current cluster bound once per pass. Repeating passes preserves the
    # original behavior where a merged cluster's expanded bounds can absorb a
    # further cluster.
    while len(clusters) > 1:
        bounds = []
        for cluster in clusters:
            bbox = fitz.Rect()
            for rect in cluster:
                bbox |= rect
            bounds.append(bbox)

        order = sorted(range(len(bounds)), key=lambda index: (bounds[index].x0, index))
        parent = list(range(len(bounds)))
        rank = [0] * len(bounds)

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left == root_right:
                return
            if rank[root_left] < rank[root_right]:
                root_left, root_right = root_right, root_left
            parent[root_right] = root_left
            if rank[root_left] == rank[root_right]:
                rank[root_left] += 1

        for position, left in enumerate(order):
            bbox_left = bounds[left]
            expanded_left = fitz.Rect(
                bbox_left.x0 - CLUSTER_MERGE_DISTANCE,
                bbox_left.y0 - CLUSTER_MERGE_DISTANCE,
                bbox_left.x1 + CLUSTER_MERGE_DISTANCE,
                bbox_left.y1 + CLUSTER_MERGE_DISTANCE,
            )
            for next_position in range(position + 1, len(order)):
                right = order[next_position]
                bbox_right = bounds[right]
                if bbox_right.x0 > expanded_left.x1:
                    break
                if expanded_left.intersects(bbox_right):
                    union(left, right)

        merged_clusters = {}
        first_index = {}
        for index, cluster in enumerate(clusters):
            root = find(index)
            merged_clusters.setdefault(root, []).extend(cluster)
            first_index.setdefault(root, index)
        next_clusters = [merged_clusters[root] for root in sorted(merged_clusters, key=first_index.get)]
        if len(next_clusters) == len(clusters):
            break
        clusters = next_clusters

    # Only keep clusters with enough drawing items to plausibly be a real
    # chart — a simple box border or a single underline is 1-4 path items,
    # a bar/line chart with axis + gridlines + data series is typically
    # dozens.
    plausible_clusters = [c for c in clusters if len(c) >= MIN_DRAWINGS_PER_CHART]

    bboxes = []
    for cluster in plausible_clusters:
        bbox = fitz.Rect()
        for r in cluster:
            bbox |= r
        bboxes.append(bbox)

    return bboxes


def _extract_vector_charts(page, page_number: int, table_regions: list[tuple] | None = None) -> list[dict]:
    """Finds vector-drawn chart regions (matplotlib-style graphs with no embedded image)."""
    drawings = page.get_drawings()
    chart_bboxes = _cluster_drawings(drawings)
    page_words = page.get_text("words")
    table_bboxes = [fitz.Rect(region) for region in (table_regions or [])]

    results = []
    for bbox in chart_bboxes:
        words_in_region = sum(
            bbox.contains(fitz.Point((word[0] + word[2]) / 2, (word[1] + word[3]) / 2))
            for word in page_words
        )
        adapted_words = [{"text": word[4], "top": word[1]} for word in page_words]
        caption = find_nearby_caption(
            adapted_words, tuple(bbox), FIGURE_CAPTION_PREFIXES, CAPTION_MAX_WORDS
        )
        table_overlap = False
        if not caption and bbox.get_area() > 0:
            for table_bbox in table_bboxes:
                intersection = fitz.Rect(bbox)
                intersection.intersect(table_bbox)
                if intersection.get_area() / bbox.get_area() >= 0.5:
                    table_overlap = True
                    break
        if not caption and (
            words_in_region > MAX_UNCAPTIONED_VECTOR_REGION_WORDS
            or bbox.width < MIN_UNCAPTIONED_VECTOR_WIDTH
            or bbox.height < MIN_UNCAPTIONED_VECTOR_HEIGHT
            or bbox.width / max(bbox.height, 1) > MAX_UNCAPTIONED_VECTOR_ASPECT_RATIO
            or table_overlap
        ):
            # Dense text and shallow vector regions are usually tables or
            # form layout, which the text/table stages already ingest. Keep
            # captioned regions eligible because captions signal a likely
            # figure even when it is small or has many labels.
            continue
        pix = page.get_pixmap(clip=bbox, matrix=fitz.Matrix(2, 2))
        results.append({
            "image_bytes": pix.tobytes("png"),
            "bbox": tuple(bbox),
            "page": page_number,
            "kind": "chart",
        })

    return results


def _select_representatives_within_cap(candidates: list[dict]) -> tuple[set[int], list[int], list[int]]:
    """
    Dedup FIRST, then cap. The cap counts unique images that actually need a
    vision-model call, so repeated logos can no longer use up the slots that
    real figures on later pages need. Returns (kept representative indices,
    skipped representative indices), both in page order.
    """
    representative_indices, index_to_representative = _deduplicate_raster_visuals(candidates)
    kept = representative_indices[:MAX_VISUALS_PER_DOCUMENT]
    skipped = representative_indices[MAX_VISUALS_PER_DOCUMENT:]
    return set(kept), skipped, index_to_representative


def extract_visuals_from_pdf(
    file_path: str,
    display_name: str | None = None,
    table_regions: list[dict] | None = None,
    on_progress=None,
) -> list[dict]:
    """
    Returns a list of visual-content chunks ready for the chunker:
    {"text": <description>, "page": ..., "source": ..., "element_type": "image"|"chart"}

    A visual whose description fails or times out still produces a chunk
    from its nearby caption when one exists. A visual with neither is
    skipped and logged by page, so it is never silently lost.
    """
    filename = display_name or file_path.split("\\")[-1].split("/")[-1]

    doc = fitz.open(file_path)
    try:
        candidates = []
        table_regions_by_page: dict[int, list[tuple]] = {}
        for table in table_regions or []:
            if table.get("bbox"):
                table_regions_by_page.setdefault(table["page"], []).append(tuple(table["bbox"]))

        for page_number, page in enumerate(doc, start=1):
            candidates.extend(_extract_raster_images(page, doc, page_number))
            candidates.extend(_extract_vector_charts(
                page, page_number, table_regions_by_page.get(page_number)
            ))

        if not candidates:
            return []

        kept_reps, skipped_reps, index_to_representative = _select_representatives_within_cap(candidates)

        if skipped_reps:
            skipped_pages = sorted({candidates[i]["page"] for i in skipped_reps})
            print(f"[visual_extractor] {len(candidates)} visual candidates; describing the first "
                  f"{MAX_VISUALS_PER_DOCUMENT} unique ones (page order). Skipped unique visuals on "
                  f"pages: {skipped_pages}", flush=True)

        # Caption lookup, computed once per page (not once per candidate).
        page_words_cache: dict[int, list[dict]] = {}

        def caption_for(candidate: dict) -> str | None:
            if not candidate["bbox"]:
                return None
            number = candidate["page"]
            if number not in page_words_cache:
                page_words_cache[number] = [
                    {"text": w[4], "top": w[1]} for w in doc[number - 1].get_text("words")
                ]
            return find_nearby_caption(
                page_words_cache[number], candidate["bbox"], FIGURE_CAPTION_PREFIXES, CAPTION_MAX_WORDS
            )

        captions = [caption_for(c) for c in candidates]

        # Only representatives within the cap are sent to the vision model.
        # Their duplicates reuse the description; the rest get no description.
        described_reps = sorted(kept_reps)
        description_by_rep: dict[int, str | None] = {}
        if described_reps:
            provider = get_vision_provider()
            descriptions = provider.describe_batch(
                [candidates[i]["image_bytes"] for i in described_reps],
                [captions[i] for i in described_reps],
                on_progress=on_progress,
            )
            description_by_rep = dict(zip(described_reps, descriptions))

        results = []
        failed_pages = []
        skipped_by_cap_pages = []
        for index, candidate in enumerate(candidates):
            representative = index_to_representative[index]
            if representative in skipped_reps:
                skipped_by_cap_pages.append(candidate["page"])
                continue

            caption = captions[index]
            description = description_by_rep.get(representative)
            if description:
                text = f"{caption}\n\n{description}" if caption else description
            elif caption:
                # Vision failed or timed out, but the figure still has a caption.
                text = caption
            else:
                failed_pages.append(candidate["page"])
                continue

            results.append({
                "text": text,
                "page": candidate["page"],
                "source": filename,
                "element_type": candidate["kind"],
            })

        if failed_pages:
            print(f"[visual_extractor] No description or caption for visuals on pages: "
                  f"{sorted(set(failed_pages))}", flush=True)
        print(f"[visual_extractor] Produced {len(results)} visual chunk(s).", flush=True)
        return results
    finally:
        doc.close()


if __name__ == "__main__":
    import sys
    test_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not test_file:
        print("Usage: python visual_extractor.py <path_to_pdf>")
    else:
        visuals = extract_visuals_from_pdf(test_file)
        print(f"Found {len(visuals)} visual(s).")
        for i, v in enumerate(visuals, start=1):
            print(f"\n--- Visual {i} (page {v['page']}, {v['element_type']}) ---")
            print(v["text"])