#!/usr/bin/env python3
"""
Company Intelligence MCP Server
Combines SEC filings + patent data + domain whois + SSL info.
$29/mo subscription.
"""

import asyncio
import json
import os
import socket
import ssl
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from typing import Any
from xml.etree import ElementTree

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
    EmbeddedResource,
)

# --- Configuration ---
FREE_LIMIT = 10
PRO_KEYS = {"demo"}  # demo key for testing
STRIPE_LINK = "https://buy.stripe.com/7sYbJ36Pl0Bm9PW6UX1oI0H"
USAGE: dict[str, int] = {}

# --- Helper: Rate Limiting ---

def check_rate_limit(key: str) -> bool:
    """Return True if key has capacity remaining."""
    now = time.time()
    # Reset stale entries
    for k in list(USAGE.keys()):
        if now - USAGE[k] > 86400:  # 24 hour window
            del USAGE[k]
    if key in PRO_KEYS:
        return True
    count = sum(1 for k, t in USAGE.items() if k == key)
    if count >= FREE_LIMIT:
        return False
    return True


def decrement_rate_limit(key: str) -> None:
    USAGE[key] = time.time()


def require_key(params: dict) -> str:
    """Extract and validate API key from params."""
    key = params.get("api_key", "anonymous")
    if not check_rate_limit(key):
        raise PermissionError(
            f"Free limit of {FREE_LIMIT} requests reached. "
            f"Upgrade at {STRIPE_LINK}"
        )
    return key


# --- Data Fetch Helpers ---

def _fetch_url(url: str, headers: dict | None = None) -> str | None:
    """Fetch a URL and return text content or None."""
    req = urllib.request.Request(
        url,
        headers=headers or {
            "User-Agent": "CompanyIntelMCP/1.0 (contact@companyintel.com)"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _sec_cik_lookup(name: str) -> str | None:
    """Look up a company CIK by name via SEC EDGAR."""
    url = f"https://efts.sec.gov/LATEST/search-index?q={urllib.parse.quote(name)}&dateRange=all&start=0&count=1"
    data = _fetch_url(url)
    if not data:
        return None
    try:
        result = json.loads(data)
        hits = result.get("hits", {}).get("hits", [])
        if hits:
            cik = hits[0].get("_id", "")
            return cik
    except Exception:
        pass
    return None


def _sec_filings(cik: str, count: int = 5) -> list[dict]:
    """Fetch recent SEC filings for a CIK."""
    cik_padded = cik.zfill(10)
    url = f"https://efts.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_padded}&type=&dateb=&owner=exclude&start=0&count={count}&output=atom"
    data = _fetch_url(url)
    if not data:
        return []
    filings = []
    try:
        root = ElementTree.fromstring(data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            updated_el = entry.find("atom:updated", ns)
            filing = {
                "title": title_el.text if title_el is not None else "",
                "link": link_el.attrib.get("href", "") if link_el is not None else "",
                "date": updated_el.text[:10] if updated_el is not None else "",
            }
            filings.append(filing)
    except Exception:
        pass
    return filings


def _sec_financials(ticker: str) -> dict:
    """Pull a simplified financial snapshot from SEC EDGAR via companyfacts."""
    # First, lookup CIK from ticker via SEC ticker mapping
    url = f"https://efts.sec.gov/files/company_tickers.json"
    data = _fetch_url(url)
    cik = None
    company_name = ticker.upper()
    if data:
        try:
            ticker_map = json.loads(data)
            for item in ticker_map.values():
                if item.get("ticker", "").upper() == ticker.upper():
                    cik = str(item["cik_str"]).zfill(10)
                    company_name = item.get("title", ticker.upper())
                    break
        except Exception:
            pass

    if not cik:
        return {"ticker": ticker.upper(), "error": "Ticker not found in SEC database"}

    # Fetch company facts
    facts_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    facts_data = _fetch_url(facts_url, headers={
        "User-Agent": "CompanyIntelMCP/1.0 (contact@companyintel.com)",
        "Accept": "application/json",
    })
    if not facts_data:
        return {"ticker": ticker.upper(), "cik": cik, "error": "No financial data available"}

    try:
        facts = json.loads(facts_data)
        us_gaap = facts.get("facts", {}).get("us-gaap", {})

        result = {
            "ticker": ticker.upper(),
            "company": company_name,
            "cik": cik,
        }

        # Try to get key financial metrics
        metric_map = {
            "Assets": ["Assets", "AssetsCurrent", "AssetsNoncurrent"],
            "Revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomer"],
            "NetIncome": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
            "Liabilities": ["Liabilities", "LiabilitiesCurrent", "LiabilitiesNoncurrent"],
            "OperatingIncome": ["OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"],
        }

        for label, gaap_keys in metric_map.items():
            for gaap_key in gaap_keys:
                if gaap_key in us_gaap:
                    units = us_gaap[gaap_key].get("units", {})
                    # Prefer USD
                    for unit_key in ["USD", "usd", "USD/shares"]:
                        if unit_key in units and len(units[unit_key]) > 0:
                            latest = units[unit_key][-1]
                            result[label] = {
                                "value": latest.get("val"),
                                "date": latest.get("end"),
                                "unit": unit_key,
                            }
                            break
                    if label in result:
                        break

        return result
    except Exception as e:
        return {"ticker": ticker.upper(), "cik": cik, "error": str(e)}


def _uspto_patents(company: str, count: int = 5) -> list[dict]:
    """Search patents via USPTO API."""
    query = urllib.parse.quote(company)
    url = f"https://patent.ic.gc.ca/opic-cipo/cpd/eng/search/patent?query={query}&start=0&num={count}&format=json"
    # Fall back to the USPTO open data API
    url = f"https://developer.uspto.gov/ibd-api/v1/patent/query?searchText=AN:{urllib.parse.quote(company)}&start=0&rows={count}"
    data = _fetch_url(url)
    if not data:
        # Try alternative endpoint
        url2 = f"https://openapi.uspto.gov/patents/v1/search?q=AN:{urllib.parse.quote(company)}&limit={count}"
        data = _fetch_url(url2)
    if not data:
        return []
    patents = []
    try:
        result = json.loads(data)
        items = result.get("results", []) or result.get("patents", []) or result.get("response", {}).get("docs", [])
        for item in items[:count]:
            patent = {
                "title": item.get("inventionTitle", item.get("title", "N/A")),
                "patent_number": item.get("patentNumber", item.get("patent_number", "N/A")),
                "date": item.get("patentDate", item.get("date", item.get("patent_date", "N/A"))),
                "status": item.get("status", item.get("patentStatus", "Published")),
            }
            patents.append(patent)
    except Exception:
        pass
    return patents


def _domain_whois(domain: str) -> dict:
    """Get basic whois info via socket connection to whois servers."""
    whois = {}
    try:
        # Use a simple whois lookup
        server = "whois.verisign-grs.com"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((server, 43))
        sock.send(f"{domain}\r\n".encode())
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        sock.close()
        text = response.decode("utf-8", errors="ignore")
        whois["raw"] = len(text)
        for line in text.split("\n"):
            lower = line.lower()
            if "creation date" in lower or "created" in lower:
                whois["created"] = line.split(": ", 1)[-1].strip() if ": " in line else line.strip()
            if "expiration date" in lower or "expiry" in lower or "expires" in lower:
                whois["expires"] = line.split(": ", 1)[-1].strip() if ": " in line else line.strip()
            if "registrar" in lower and ":" in line:
                whois["registrar"] = line.split(": ", 1)[-1].strip()
            if "name server" in lower or "nameserver" in lower:
                ns = line.split(": ", 1)[-1].strip() if ": " in line else line.strip()
                if ns and ns != domain:
                    whois.setdefault("nameservers", []).append(ns)
    except Exception as e:
        whois["error"] = str(e)

    # Try DNS records
    dns_info = {}
    try:
        dns_info["a_record"] = socket.gethostbyname(domain)
    except Exception:
        dns_info["a_record"] = "unresolvable"

    # SSL info
    ssl_info = {}
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
                if cert:
                    ssl_info["subject"] = dict(cert.get("subject", [])).get("commonName", "")
                    ssl_info["issuer"] = dict(cert.get("issuer", [])).get("organizationName", "")
                    ssl_info["expiry"] = cert.get("notAfter", "")
                    ssl_info["valid"] = True
    except Exception:
        ssl_info["valid"] = False
        ssl_info["error"] = "Could not establish SSL connection"

    return {
        "domain": domain,
        "whois": whois,
        "dns": dns_info,
        "ssl": ssl_info,
    }


# --- MCP Server Setup ---

app = Server("company-intel-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="company_profile",
            description="Get a comprehensive company profile: SEC filings, patents, and domain/whois info.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Company name or domain"},
                    "api_key": {"type": "string", "description": "API key (optional, defaults to anonymous)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="company_financials",
            description="Pull financial data for a publicly traded company via SEC EDGAR.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g., AAPL, MSFT)"},
                    "api_key": {"type": "string", "description": "API key (optional)"},
                },
                "required": ["ticker"],
            },
        ),
        Tool(
            name="company_patents",
            description="Search patents by company name via USPTO data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Company name to search patents for"},
                    "count": {"type": "integer", "description": "Number of results (max 20)", "default": 5},
                    "api_key": {"type": "string", "description": "API key (optional)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="company_domain",
            description="Get whois, DNS, and SSL certificate information for a domain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "Domain name (e.g., example.com)"},
                    "api_key": {"type": "string", "description": "API key (optional)"},
                },
                "required": ["domain"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent | ImageContent | EmbeddedResource]:
    try:
        key = require_key(arguments)
    except PermissionError as e:
        return [TextContent(type="text", text=str(e))]

    decrement_rate_limit(key)

    if name == "company_profile":
        query = arguments.get("name", "")
        result = {"name": query}

        # Try as domain first
        if "." in query and not query.startswith("http"):
            result["domain_info"] = _domain_whois(query)

        # SEC lookup
        cik = _sec_cik_lookup(query)
        if cik:
            result["cik"] = cik
            result["sec_filings"] = _sec_filings(cik)
        else:
            result["sec_filings"] = []

        # Patents
        result["patents"] = _uspto_patents(query)

        data = json.dumps(result, indent=2)

    elif name == "company_financials":
        ticker = arguments.get("ticker", "")
        result = _sec_financials(ticker)
        data = json.dumps(result, indent=2)

    elif name == "company_patents":
        company = arguments.get("name", "")
        count = min(int(arguments.get("count", 5)), 20)
        patents = _uspto_patents(company, count)
        data = json.dumps({"company": company, "patents": patents, "count": len(patents)}, indent=2)

    elif name == "company_domain":
        domain = arguments.get("domain", "")
        result = _domain_whois(domain)
        data = json.dumps(result, indent=2)

    else:
        data = json.dumps({"error": f"Unknown tool: {name}"})

    return [TextContent(type="text", text=data)]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
