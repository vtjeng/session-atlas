"""Rough per-model cost estimates from token usage.

Rates are US-dollar list prices per 1,000,000 tokens, current as of ``AS_OF``.
They are estimates, not invoices: they use each vendor's standard published
list rates (short-context, non-batch, no priority surcharge), ignore
volume/enterprise discounts and long-context tiers, and drift whenever a vendor
changes pricing. Update the table below (and ``AS_OF``) when rates move.

Each entry is ``(input, output, cache_read, cache_write, cache_write_1h)``
per 1M tokens:
  input        uncached prompt tokens, full rate
  output       generated tokens
  cache_read   tokens served from cache (cheaper): Anthropic 0.1x input
               (0.025x on Fable 5.1 and Mythos 5.1, 0.05x on Opus 5.5), OpenAI
               its published cached-input rate
  cache_write  standard writes: Anthropic 5-minute TTL and OpenAI's 30-minute
               TTL are 1.25x input.
  cache_write_1h  Anthropic's extended one-hour TTL, at 2x input.
``None`` marks a category the vendor doesn't bill for that model, such as a
cache write on an OpenAI model with no published cache-write price, so it's
never priced.

``PRICES`` lists every model on both vendors' standard pricing tables, and the
site shows only the rates of models that a page's sessions used.
"""

AS_OF = "2026-09-29"

# Anthropic: platform.claude.com/docs (standard rates).
# OpenAI: developers.openai.com/api/docs/pricing (standard short-context rates).
PRICES = {
    "claude-fable-5-1":(10.00, 50.00, 0.250, 12.50, 20.00),
    "claude-fable-5":  (10.00, 50.00, 1.000, 12.50, 20.00),
    "claude-mythos-5-1":
                         (10.00, 50.00, 0.250, 12.50, 20.00),
    "claude-mythos-5": (10.00, 50.00, 1.000, 12.50, 20.00),
    "claude-opus-5-5": ( 4.00, 20.00, 0.200,  5.00,  8.00),
    "claude-opus-5":   ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-8": ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-7": ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-6": ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-5": ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-5-20251101":
                         ( 5.00, 25.00, 0.500,  6.25, 10.00),
    "claude-opus-4-1": (15.00, 75.00, 1.500, 18.75, 30.00),
    "claude-opus-4-1-20250805":
                         (15.00, 75.00, 1.500, 18.75, 30.00),
    "claude-opus-4-0": (15.00, 75.00, 1.500, 18.75, 30.00),
    "claude-opus-4-20250514":
                         (15.00, 75.00, 1.500, 18.75, 30.00),
    "claude-sonnet-5-5":(2.00, 10.00, 0.200,  2.50,  4.00),
    "claude-sonnet-5": ( 2.00, 10.00, 0.200,  2.50,  4.00),
    "claude-sonnet-4-6":
                         ( 3.00, 15.00, 0.300,  3.75,  6.00),
    "claude-sonnet-4-5":
                         ( 3.00, 15.00, 0.300,  3.75,  6.00),
    "claude-sonnet-4-5-20250929":
                         ( 3.00, 15.00, 0.300,  3.75,  6.00),
    "claude-sonnet-4-0":
                         ( 3.00, 15.00, 0.300,  3.75,  6.00),
    "claude-sonnet-4-20250514":
                         ( 3.00, 15.00, 0.300,  3.75,  6.00),
    "claude-haiku-4-5":( 1.00,  5.00, 0.100,  1.25,  2.00),
    "claude-haiku-4-5-20251001":
                         ( 1.00,  5.00, 0.100,  1.25,  2.00),
    "claude-3-5-haiku-20241022":
                         ( 0.80,  4.00, 0.080,  1.00,  1.60),
    "gpt-6-astra":     (10.00, 50.00, 1.000, 12.50, None),
    "gpt-6.1-sol":     ( 2.00, 10.00, 0.100,  2.50, None),
    "gpt-6-sol":       ( 2.00, 10.00, 0.200,  2.50, None),
    "gpt-6-luna":      ( 0.10,  0.50, 0.010,  0.125, None),
    "gpt-5.6":         ( 4.00, 20.00, 0.400,  5.00, None),
    "gpt-5.6-sol":     ( 4.00, 20.00, 0.400,  5.00, None),
    "gpt-5.6-terra":   ( 2.00, 12.00, 0.200,  2.50, None),
    "gpt-5.6-luna":    ( 0.20,  1.20, 0.020,  0.25, None),
    "gpt-5.6-cyber":   (12.50, 75.00, 1.250, 15.625,  None),
    "gpt-5.5-pro":     (30.00, 180.00,  None,  None,  None),
    "gpt-5.5":         ( 5.00, 30.00, 0.500,  None,  None),
    "gpt-5.5-cyber":   (12.50, 75.00, 1.250,  None,  None),
    "gpt-5.4-pro":     (30.00, 180.00,  None,  None,  None),
    "gpt-5.4":         ( 2.50, 15.00, 0.250,  None,  None),
    "gpt-5.4-mini":    ( 0.75,  4.50, 0.075,  None,  None),
    "gpt-5.4-nano":    ( 0.20,  1.25, 0.020,  None,  None),
    "gpt-5.3-codex":   ( 1.75, 14.00, 0.175,  None,  None),
    "gpt-5.2-pro":     (21.00, 168.00,  None,  None,  None),
    "gpt-5.2":         ( 1.75, 14.00, 0.175,  None,  None),
    "gpt-5.1":         ( 1.25, 10.00, 0.125,  None,  None),
    "gpt-5-pro":       (15.00, 120.00,  None,  None,  None),
    "gpt-5":           ( 1.25, 10.00, 0.125,  None,  None),
    "gpt-5-mini":      ( 0.25,  2.00, 0.025,  None,  None),
    "gpt-5-nano":      ( 0.05,  0.40, 0.005,  None,  None),
    "gpt-5-search-api":( 1.25, 10.00, 0.125,  None,  None),
    "chat-latest":     ( 5.00, 30.00, 0.500,  None,  None),
    "gpt-rosalind-research":
                         ( 5.00, 25.00, 0.500,  None,  None),
    "gpt-4.1":         ( 2.00,  8.00, 0.500,  None,  None),
    "gpt-4.1-mini":    ( 0.40,  1.60, 0.100,  None,  None),
    "gpt-4.1-nano":    ( 0.10,  0.40, 0.025,  None,  None),
    "gpt-4o":          ( 2.50, 10.00, 1.250,  None,  None),
    "gpt-4o-2024-05-13":
                         ( 5.00, 15.00,  None,  None,  None),
    "gpt-4o-mini":     ( 0.15,  0.60, 0.075,  None,  None),
    "o4-mini":         ( 1.10,  4.40, 0.275,  None,  None),
    "o3-pro":          (20.00, 80.00,  None,  None,  None),
    "o3":              ( 2.00,  8.00, 0.500,  None,  None),
    "o3-mini":         ( 1.10,  4.40, 0.550,  None,  None),
    "o1-pro":          (150.00, 600.00,  None,  None,  None),
    "o1":              (15.00, 60.00, 7.500,  None,  None),
    "gpt-4-turbo-2024-04-09":
                         (10.00, 30.00,  None,  None,  None),
    "gpt-4-0613":      (30.00, 60.00,  None,  None,  None),
    "gpt-3.5-turbo":   ( 0.50,  1.50,  None,  None,  None),
    "gpt-3.5-turbo-0125":
                         ( 0.50,  1.50,  None,  None,  None),
    "gpt-3.5-turbo-1106":
                         ( 1.00,  2.00,  None,  None,  None),
    "gpt-3.5-turbo-instruct":
                         ( 1.50,  2.00,  None,  None,  None),
    "davinci-002":     ( 2.00,  2.00,  None,  None,  None),
    "babbage-002":     ( 0.40,  0.40,  None,  None,  None),
}


# Token categories in rate-tuple order. This data also generates the accounting
# panel's labels and explanations.
CATEGORY_SPECS = (
    ("in", "input",
     "Uncached prompt tokens billed at the full input rate. Codex reports total "
     "input including cached input, so the parser subtracts the cached portion."),
    ("out", "output", "Tokens generated by the model."),
    ("cr", "cache read", "Previously cached input billed at the cache-read rate."),
    ("cc", "cache write",
     "Standard cache writes: five minutes for Anthropic and 30 minutes for OpenAI."),
    ("cc1h", "cache write 1h",
     "Anthropic's extended one-hour cache writes; other models do not bill "
     "this category."),
)
CATEGORIES = tuple((key, label) for key, label, _ in CATEGORY_SPECS)


def cost_breakdown(by_model):
    """Break a per-model token count down into cost by token category.

    ``by_model`` maps a model id to ``{"in", "out", "cr", "cc", "cc1h"}``
    token counts for uncached input, output, cache read, standard cache write,
    and one-hour cache write. Returns
    ``(cats, total, unpriced)`` where ``cats`` maps each category key to
    ``{"tokens", "cost"}`` summed across models, ``total`` is the dollar sum,
    and ``unpriced`` is the sorted list of model ids that carried billable
    tokens but have no rate in ``PRICES`` — surface it so the estimate is never
    silently understated. A ``None`` rate means that the model does not bill
    that token category.
    """
    cats = {k: {"tokens": 0, "cost": 0.0} for k, _ in CATEGORIES}
    unpriced = []
    for mid, tk in by_model.items():
        rates = PRICES.get(mid)
        if rates is None:
            if any(tk.get(k, 0) for k, _ in CATEGORIES):
                unpriced.append(mid)
            continue
        for (k, _), rate in zip(CATEGORIES, rates):
            n = tk.get(k, 0)
            cats[k]["tokens"] += n
            if rate is not None:  # None = vendor doesn't bill this category
                cats[k]["cost"] += n * rate / 1_000_000
    total = sum(c["cost"] for c in cats.values())
    return cats, total, sorted(unpriced)


def estimate_cost(by_model):
    """Total dollar cost for a per-model token breakdown.

    See :func:`cost_breakdown` for the shape of ``by_model`` and, when you also
    need the per-category split or the unpriced-model list, call it directly.
    """
    return cost_breakdown(by_model)[1]
