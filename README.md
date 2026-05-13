# Company Intelligence MCP

**One MCP server** combining SEC filings + patent data + domain whois + SSL info. $29/mo.

## Tools

| Tool | Description |
|------|-------------|
| `company_profile(name/domain)` | Returns SEC filings, patents, and domain info |
| `company_financials(ticker)` | Pulls financials from SEC EDGAR |
| `company_patents(name)` | Searches USPTO patents |
| `company_domain(domain)` | WHOIS + SSL + DNS lookup |

## Quick Start

```bash
pip install mcp
python server.py
```

### Claude Desktop Config

```json
{
  "mcpServers": {
    "company-intel": {
      "command": "python",
      "args": ["/path/to/company-intel-mcp/server.py"]
    }
  }
}
```

## Pricing

- **Free**: 10 requests/month (no API key needed)
- **Pro**: $29/month — 250 requests/month + priority support

[Subscribe](https://buy.stripe.com/eVq28t8Xt83O9PWenp1oI0G)

## Data Sources

- SEC filings & financials: [SEC EDGAR](https://www.sec.gov/edgar)
- Patent data: [USPTO](https://www.uspto.gov/)
- WHOIS: Direct socket lookup
- DNS/SSL: Python stdlib
