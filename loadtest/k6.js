// 90% reads, 10% writes, ramping to 100 virtual users.
// Run it at each stage and compare p95 latency and error rate.
import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8000';
const USERS = 500;

export const options = {
  stages: [
    { duration: '20s', target: 20 },
    { duration: '40s', target: 100 },
    { duration: '20s', target: 0 },
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const user = `user${Math.floor(Math.random() * USERS)}`;
  if (Math.random() < 0.1) {
    const res = http.post(`${BASE}/users/${user}/posts`, { body: `hello at ${Date.now()}` },
      { tags: { name: 'create_post' } });
    check(res, { 'created': (r) => r.status === 201 });
  } else {
    const res = http.get(`${BASE}/users/${user}/posts`, { tags: { name: 'list_posts' } });
    check(res, { 'ok': (r) => r.status === 200 });
  }
  sleep(0.1);
}
