export interface Worker {
  name: string;
  machine_name: string;
  relay_port: number;
  model: string;
  repos: string[];
  status: string;
  session_id: string | null;
  github_level: string;
  created_at: string;
  last_prompt: string | null;
  last_prompt_at: string | null;
  connected: boolean;
  agent_backend?: string; // "claude" (default) | "prime" 
  // enriched by detail endpoint
  provider?: string;
  machine_ip?: string;
  relay_alive?: boolean;
  message_count?: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
  total_cost_usd?: number;
}

export interface Machine {
  name: string;
  provider: string;
  ip: string;
  region: string;
  instance_type: string;
  vm_type: string;
  ssh_user: string;
  ssh_host?: string;
  container_id: string;
  hostname?: string;
  os_info?: string;
  status: string;
  worker_names: string[];
  next_relay_port: number;
  created_at: string;
  connected?: boolean;
}

export interface ContentBlock {
  type: string;
  text?: string;
  thinking?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  tool_id?: string;
  content?: string;
  is_error?: boolean;
  raw?: string;
}

export interface Message {
  type: string;
  role: string;
  timestamp?: string;
  content?: ContentBlock[] | string;
  model?: string;
  subtype?: string;
  session_id?: string;
  total_cost_usd?: number;
  is_error?: boolean;
  num_turns?: number;
  duration_ms?: number;
}

export interface FleetConfig {
  provider: string;
  model: string;
  region: string;
  ssh_user: string;
  instance_type: string;
  vm_type: string;
  providers: Record<
    string,
    {
      region: string;
      instance_type: string;
      skus: Record<string, string>;
    }
  >;
}

export interface TaskInfo {
  id: string;
  operation: string;
  worker_name: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  error: string | null;
}
