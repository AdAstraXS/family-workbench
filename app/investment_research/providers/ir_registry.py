"""The approved issuer catalogue; identifiers are market-qualified, never ticker guesses."""
from dataclasses import dataclass


@dataclass(frozen=True)
class IRCompany:
    key: str
    name: str
    symbol: str
    market: str
    currency: str
    adapter: str
    entry: str
    assets: tuple = ()  # Exact host plus path prefix; shared CDNs are tenant-scoped.
    aliases: tuple = ()

    @property
    def identities(self):
        return ((self.market, self.symbol),) + self.aliases

    @property
    def homepage(self):
        return 'https://www.apple.com/newsroom/' if self.key == 'aapl' else self.entry


COMPANIES = (
    IRCompany('msft', 'Microsoft 微软', 'MSFT', 'US', 'USD', 'microsoft',
              'https://www.microsoft.com/en-us/investor/',
              (('cdn-dynmedia-1.microsoft.com', '/is/content/microsoftcorp/'),
               ('www.microsoft.com', '/investor/'))),
    IRCompany('aapl', 'Apple 苹果', 'AAPL', 'US', 'USD', 'apple',
              'https://www.apple.com/newsroom/sitemap.xml',
              (('www.apple.com', '/newsroom/'),)),
    IRCompany('alphabet', 'Alphabet 谷歌', 'GOOGL', 'US', 'USD', 'q4',
              'https://abc.xyz/investor/earnings/',
              (('s206.q4cdn.com', '/479360582/'),), (('US', 'GOOG'),)),
    IRCompany('amzn', 'Amazon 亚马逊', 'AMZN', 'US', 'USD', 'q4',
              'https://ir.aboutamazon.com/quarterly-results/default.aspx',
              (('s2.q4cdn.com', '/299287126/'),)),
    IRCompany('meta', 'Meta', 'META', 'US', 'USD', 'q4',
              'https://investor.atmeta.com/financials/default.aspx',
              (('s21.q4cdn.com', '/399680738/'),)),
    IRCompany('nvda', 'NVIDIA 英伟达', 'NVDA', 'US', 'USD', 'q4',
              'https://investor.nvidia.com/financial-info/financial-reports/default.aspx',
              (('s201.q4cdn.com', '/141608511/'), ('nvidianews.nvidia.com', '/'))),
    IRCompany('tsla', 'Tesla 特斯拉', 'TSLA', 'US', 'USD', 'tesla',
              'https://ir.tesla.com/',
              (('digitalassets.tesla.com', '/tesla-contents/'),
               ('assets-ir.tesla.com', '/'), ('www.tesla.com', '/ns_videos/'))),
    IRCompany('tsmc', 'TSMC 台积电', 'TSM', 'US', 'USD', 'tsmc',
              'https://investor.tsmc.com/english/quarterly-results',
              (('investor.tsmc.com', '/english/'), ('investor.cld.tsmc.com', '/english/'),
               ('pr.tsmc.com', '/english/')),
              (('TW', '2330'), ('TW', '2330.TW'))),
    IRCompany('spacex', 'SpaceX', 'SPCX', 'US', 'USD', 'q4',
              'https://ir.spacex.com/financials/',
              (('s21.q4cdn.com', '/184289198/'), ('content.spacex.com', '/cms-assets/'))),
    IRCompany('skhynix', 'SK 海力士', '000660', 'KR', 'KRW', 'skhynix',
              'https://www.skhynix.com/ir/UI-FR-IR01/',
              (('homeapi.skhynix.com', '/board/'), ('news.skhynix.com', '/'),
               ('mis-prod-koce-homepage-cdn-01-blob-ep.azureedge.net', '/web/attach/')),
              (('KR', '000660.KS'),)),
    IRCompany('avgo', 'Broadcom 博通', 'AVGO', 'US', 'USD', 'broadcom',
              'https://investors.broadcom.com/financial-information/quarterly-results'),
    IRCompany('amd', 'AMD', 'AMD', 'US', 'USD', 'q4inc',
              'https://ir.amd.com/financial-information/financial-results',
              (('d1io3yog0oux5.cloudfront.net', '/@issuer/amd/'),)),
    IRCompany('intc', 'Intel 英特尔', 'INTC', 'US', 'USD', 'q4inc',
              'https://www.intc.com/financial-info/financial-results',
              (('d1io3yog0oux5.cloudfront.net', '/@issuer/intel/'),)),
)
BY_KEY = {company.key: company for company in COMPANIES}


def company_for_security(security):
    if security.asset_type != 'stock':
        return None
    identity = (security.market.upper(), security.symbol.upper())
    return next((company for company in COMPANIES if identity in company.identities), None)
