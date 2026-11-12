// Crawl the bench site with Crawlee's CheerioCrawler and print one RESULT line.
// Usage: node crawlee_crawl.mjs <base-url> <defaults|matched> <max-pages>
import { CheerioCrawler, Configuration, log } from 'crawlee';

log.setLevel(log.LEVELS.OFF);
const [base, mode, maxPages] = process.argv.slice(2);
const items = [];
const options = {
  maxRequestsPerCrawl: Number(maxPages),
  async requestHandler({ request, $, enqueueLinks }) {
    items.push({ url: request.loadedUrl ?? request.url, title: $('title').text() });
    await enqueueLinks();
  },
};
if (mode === 'matched') {
  // same load budget as the ww-scraper run, and robots.txt on like ww-scraper's default
  Object.assign(options, { maxConcurrency: 10, respectRobotsTxtFile: true });
}
const crawler = new CheerioCrawler(options, new Configuration({ persistStorage: false }));
await crawler.run([base + '/']);
console.log('RESULT ' + JSON.stringify({ items }));
