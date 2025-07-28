import { WebSocketServer } from 'ws';

const port = process.env.PORT || 3000;
const wss = new WebSocketServer({ port });

const clients = new Set();

wss.on('connection', (ws) => {
  console.log('Client connected');
  clients.add(ws);

  ws.on('message', (message) => {
    console.log('Received:', message.toString());

    // Broadcast to all clients
    for (const client of clients) {
      if (client.readyState === ws.OPEN) {
        client.send(message.toString());
      }
    }
  });

  ws.on('close', () => {
    console.log('Client disconnected');
    clients.delete(ws);
  });
});

console.log(`WebSocket server running on port ${port}`);
