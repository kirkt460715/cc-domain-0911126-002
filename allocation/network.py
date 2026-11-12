from dataclasses import dataclass
from collections import deque
import heapq


@dataclass
class Arc:
    target: int
    reverse: int
    capacity: int
    cost: int
    initial: int
    label: object = None


class Network:
    def __init__(self):
        self.names = []
        self.indices = {}
        self.graph = []
        self.forward = []

    def node(self, name):
        if name not in self.indices:
            self.indices[name] = len(self.names)
            self.names.append(name)
            self.graph.append([])
        return self.indices[name]

    def add(self, source, target, capacity, cost=0, label=None):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
            raise ValueError('capacity must be nonnegative integer')
        if isinstance(cost, bool) or not isinstance(cost, int):
            raise ValueError('cost must be integer')
        u, v = self.node(source), self.node(target)
        if u == v:
            raise ValueError('self loops are not supported')
        forward = Arc(v, len(self.graph[v]), capacity, cost, capacity, label)
        backward = Arc(u, len(self.graph[u]), 0, -cost, 0)
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        handle = (u, len(self.graph[u]) - 1)
        self.forward.append(handle)
        return handle

    def flow(self, handle):
        u, i = handle
        arc = self.graph[u][i]
        return arc.initial - arc.capacity

    def reset(self):
        for u, i in self.forward:
            arc = self.graph[u][i]
            arc.capacity = arc.initial
            self.graph[arc.target][arc.reverse].capacity = 0

    def reachable(self, source):
        if source not in self.indices:
            return set()
        start = self.indices[source]
        visited = {start}
        queue = deque([start])
        while queue:
            u = queue.popleft()
            for edge in self.graph[u]:
                if edge.capacity > 0 and edge.target not in visited:
                    visited.add(edge.target)
                    queue.append(edge.target)
        return {self.names[i] for i in visited}

    def cut(self, source):
        reachable = self.reachable(source)
        result = []
        for u, i in self.forward:
            edge = self.graph[u][i]
            if self.names[u] in reachable and self.names[edge.target] not in reachable:
                if edge.initial:
                    result.append({
                        'source': self.names[u],
                        'target': self.names[edge.target],
                        'capacity': edge.initial,
                        'flow': self.flow((u, i)),
                        'label': edge.label,
                    })
        return result

    def verify(self, source, sink):
        balance = [0] * len(self.names)
        cost = 0
        for u, i in self.forward:
            edge = self.graph[u][i]
            amount = self.flow((u, i))
            if not 0 <= amount <= edge.initial:
                raise AssertionError('flow violates capacity')
            reverse = self.graph[edge.target][edge.reverse]
            if reverse.capacity != amount:
                raise AssertionError('residual reverse arc mismatch')
            balance[u] -= amount
            balance[edge.target] += amount
            cost += amount * edge.cost
        s, t = self.indices[source], self.indices[sink]
        if any(value for i, value in enumerate(balance) if i not in (s, t)):
            raise AssertionError('flow conservation failed')
        if balance[s] + balance[t] or balance[t] < 0:
            raise AssertionError('source/sink imbalance')
        return {'flow': balance[t], 'cost': cost}

    def max_flow(self, source, sink, limit=None):
        s, t = self.node(source), self.node(sink)
        remaining = sum(e.capacity for e in self.graph[s]) if limit is None else limit
        if not isinstance(remaining, int) or remaining < 0:
            raise ValueError('invalid flow limit')
        total = 0
        while remaining:
            parent = [None] * len(self.graph)
            parent[s] = (-1, -1)
            queue = deque([s])
            while queue and parent[t] is None:
                u = queue.popleft()
                for i, edge in enumerate(self.graph[u]):
                    if edge.capacity > 0 and parent[edge.target] is None:
                        parent[edge.target] = (u, i)
                        queue.append(edge.target)
            if parent[t] is None:
                break
            amount = remaining
            v = t
            while v != s:
                u, i = parent[v]
                amount = min(amount, self.graph[u][i].capacity)
                v = u
            v = t
            while v != s:
                u, i = parent[v]
                edge = self.graph[u][i]
                edge.capacity -= amount
                self.graph[v][edge.reverse].capacity += amount
                v = u
            total += amount
            remaining -= amount
        return total

    def _initial_potential(self, start):
        distance = [float('inf')] * len(self.graph)
        distance[start] = 0
        for iteration in range(len(self.graph)):
            changed = False
            for u, edges in enumerate(self.graph):
                if distance[u] == float('inf'):
                    continue
                for edge in edges:
                    if edge.capacity and distance[edge.target] > distance[u] + edge.cost:
                        distance[edge.target] = distance[u] + edge.cost
                        changed = True
            if not changed:
                return [0 if d == float('inf') else d for d in distance]
        raise ValueError('reachable negative cost cycle')

    def min_cost_flow(self, source, sink, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError('invalid flow limit')
        s, t = self.node(source), self.node(sink)
        potential = self._initial_potential(s)
        total = 0
        cost = 0
        while total < limit:
            distance = [float('inf')] * len(self.graph)
            parent = [None] * len(self.graph)
            distance[s] = 0
            heap = [(0, s)]
            while heap:
                dist, u = heapq.heappop(heap)
                if dist != distance[u]:
                    continue
                for i, edge in enumerate(self.graph[u]):
                    if edge.capacity <= 0:
                        continue
                    reduced = edge.cost + potential[u] - potential[edge.target]
                    if reduced < 0:
                        raise AssertionError('invalid reduced cost')
                    candidate = dist + reduced
                    if candidate < distance[edge.target]:
                        distance[edge.target] = candidate
                        parent[edge.target] = (u, i)
                        heapq.heappush(heap, (candidate, edge.target))
            if parent[t] is None:
                break
            for i, value in enumerate(distance):
                if value != float('inf'):
                    potential[i] += value
            amount = limit - total
            v = t
            while v != s:
                u, i = parent[v]
                amount = min(amount, self.graph[u][i].capacity)
                v = u
            v = t
            path_cost = 0
            while v != s:
                u, i = parent[v]
                edge = self.graph[u][i]
                edge.capacity -= amount
                self.graph[v][edge.reverse].capacity += amount
                path_cost += edge.cost
                v = u
            total += amount
            cost += amount * path_cost
        return total, cost
