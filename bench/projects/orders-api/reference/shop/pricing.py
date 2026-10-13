VAT_PERCENT = 20


def vat_cents(net_cents):
    """ДДС веднъж върху общата сума, до цент, половин цент нагоре — само с цели числа."""
    return (net_cents * VAT_PERCENT * 2 + 100) // 200


def totals(lines):
    net = sum(line["net_cents"] for line in lines)
    vat = vat_cents(net)
    return {"net_cents": net, "vat_cents": vat, "total_cents": net + vat}
