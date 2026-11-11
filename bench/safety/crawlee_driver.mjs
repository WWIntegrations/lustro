// Runs one scenario with Crawlee's CheerioCrawler and prints one JSON line.
// Usage: node crawlee_driver.mjs <base-url> <robots|ssrf|retry|csv> <default|robots-on>
import { CheerioCrawler, Configuration, Dataset, KeyValueStore, log } from 'crawlee';

log.setLevel(log.LEVELS.OFF);
const [base, scenario, mode] = process.argv.slice(2);
const config = new Configuration({ persistStorage: false });
const out = { visited: [], errors: [] };

if (scenario === 'csv') {
  const ds = await Dataset.open('bench', { config });
  await ds.pushData({ name: '=HYPERLINK("http://evil.example","click")', n: 1 });
  await ds.exportToCSV('OUT', { toKVS: 'bench-kvs' });
  const kvs = await KeyValueStore.open('bench-kvs', { config });
  out.csv = String(await kvs.getValue('OUT'));
} else {
  const crawler = new CheerioCrawler({
    respectRobotsTxtFile: mode === 'robots-on',
    maxRequestsPerCrawl: 30,
    navigationTimeoutSecs: 5, // the only non-default: keeps an unreachable host from stalling the run
    async requestHandler({ request, enqueueLinks }) {
      out.visited.push(request.loadedUrl ?? request.url);
      if (scenario === 'robots') await enqueueLinks();
    },
    failedRequestHandler({ request }, error) {
      out.errors.push({ url: request.url, error: String(error?.message ?? error).slice(0, 160) });
    },
  }, config);
  const start = { robots: '/', ssrf: '/redirect-ssrf', retry: '/slow-429' }[scenario];
  await crawler.run([base + start]);
}
console.log('RESULT ' + JSON.stringify(out));
