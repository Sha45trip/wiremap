export async function GET() {
  return Response.json([]);
}

// planted: exported POST with no auth check -> missing_auth
export async function POST(req) {
  return Response.json({ id: 1 });
}
