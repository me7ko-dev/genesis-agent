from fuel import cheapest, parse_prices

HTML = """<html><body>
<table class="ads"><tr><td>Реклама</td><td>1,00 лв.</td></tr></table>
<table class="prices">
  <thead><tr><th>Бензиностанция</th><th>А95</th><th>Дизел</th><th>LPG</th></tr></thead>
  <tbody>
    <tr><td> Shell Бояна </td><td>2,59 лв.</td><td>2,49 лв.</td><td>1,19 лв.</td></tr>
    <tr><td>OMV Люлин</td><td>2,55 лв.</td><td>2,52 лв.</td><td>-</td></tr>
    <tr><td>Lukoil Център</td><td>-</td><td>2,39 лв.</td><td>1,15 лв.</td></tr>
  </tbody>
</table></body></html>"""

def test_parse():
    rows = parse_prices(HTML)
    assert rows == [
        {"station": "Shell Бояна", "a95": 2.59, "diesel": 2.49, "lpg": 1.19},
        {"station": "OMV Люлин", "a95": 2.55, "diesel": 2.52, "lpg": None},
        {"station": "Lukoil Център", "a95": None, "diesel": 2.39, "lpg": 1.15},
    ]

def test_cheapest():
    rows = parse_prices(HTML)
    assert cheapest(rows, "a95")["station"] == "OMV Люлин"
    assert cheapest(rows, "diesel")["station"] == "Lukoil Център"
    assert cheapest(rows, "lpg")["station"] == "Lukoil Център"
