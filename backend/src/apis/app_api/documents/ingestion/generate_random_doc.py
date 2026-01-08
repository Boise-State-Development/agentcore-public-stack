import random

def generate_big_doc():
    filename = "titan_voyager_stress_test.txt"
    
    # Standard filler to bulk up token count
    # 100 words of "Lorem Ipsum" style technical jargon
    filler_vocab = [
        "vector", "embedding", "latency", "throughput", "s3", "bucket", "optimization",
        "scalable", "redundancy", "availability", "zone", "region", "compute", "node",
        "cluster", "shard", "replica", "consistency", "eventual", "strong", "acid",
        "transaction", "rollback", "commit", "deploy", "pipeline", "ci/cd", "git",
        "merge", "branch", "main", "production", "staging", "development", "local"
    ]
    
    def get_filler(word_count=500):
        return " ".join(random.choices(filler_vocab, k=word_count))

    content = []
    
    # --- HEADER ---
    content.append("# PROJECT TITAN VOYAGER: ARCHITECTURE SPECIFICATION")
    content.append("CONFIDENTIAL - DO NOT DISTRIBUTE")
    content.append("DATE: 2025-12-12")
    content.append("=" * 50 + "\n")

    # --- SECTION 1: SECURITY (Chunk 1 target) ---
    content.append("## SECTION 1: SECURITY PROTOCOLS")
    content.append("This section defines the authentication mechanisms.")
    content.append("CRITICAL: The master override password for the firewall is 'Solar-Flare-88'.")
    content.append("(Begin Filler Content for Section 1)")
    content.append(get_filler(1200)) # ~1500 tokens of filler
    content.append("(End Section 1)\n")
    
    # --- SECTION 2: INFRASTRUCTURE (Chunk 2 target) ---
    content.append("## SECTION 2: INFRASTRUCTURE TOPOLOGY")
    content.append("This section outlines the AWS region strategy.")
    content.append("NOTE: The primary database is located in us-west-2, distinct from the backup in eu-central-1.")
    content.append("CRITICAL: The database read-replica port has been changed to 5439 to avoid conflicts.")
    content.append("(Begin Filler Content for Section 2)")
    content.append(get_filler(1200)) # ~1500 tokens of filler
    content.append("(End Section 2)\n")

    # --- SECTION 3: COMPLIANCE (Chunk 3 target) ---
    content.append("## SECTION 3: HUMAN RESOURCES & COMPLIANCE")
    content.append("This section details the employee code of conduct.")
    content.append("CRITICAL: All PTO requests must be submitted exactly 14 days in advance via the Portal.")
    content.append("(Begin Filler Content for Section 3)")
    content.append(get_filler(1200)) # ~1500 tokens of filler
    content.append("(End Section 3)\n")
    
    # --- SECTION 4: LEGACY SYSTEMS (Chunk 4 target) ---
    content.append("## SECTION 4: DEPRECATION SCHEDULE")
    content.append("This section lists systems slated for removal.")
    content.append("CRITICAL: The COBOL mainframe (System Z) is scheduled for hard shutdown on 2029-01-01.")
    content.append("(Begin Filler Content for Section 4)")
    content.append(get_filler(1200)) # ~1500 tokens of filler
    content.append("END OF DOCUMENT")

    # Write to file
    full_text = "\n\n".join(content)
    with open(filename, "w", encoding='utf-8') as f:
        f.write(full_text)
    
    print(f"✅ Generated {filename}")
    print(f"📊 Size: {len(full_text)} characters (approx {len(full_text)/4:.0f} tokens)")
    print("🚀 Ready to upload!")

if __name__ == "__main__":
    generate_big_doc()