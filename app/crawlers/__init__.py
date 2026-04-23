from app.crawlers.base import BaseCrawler, CrawlerRegistry
from app.crawlers.remoteok import RemoteOKCrawler
from app.crawlers.weworkremotely import WeWorkRemotelyCrawler

registry = CrawlerRegistry()
registry.register(RemoteOKCrawler())
registry.register(WeWorkRemotelyCrawler())

__all__ = ["BaseCrawler", "CrawlerRegistry", "registry"]
