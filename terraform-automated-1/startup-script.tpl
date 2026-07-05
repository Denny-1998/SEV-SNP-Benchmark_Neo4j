#!/bin/bash
set -euxo pipefail

CORES="${cores}"
VARIANT="${variant}"

# --- Install packages --------------------------------------------------------
apt-get update
apt-get install -y ca-certificates curl gnupg git openjdk-11-jdk maven sysstat

# --- Docker ------------------------------------------------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker
chmod 666 /var/run/docker.sock

# --- Directory structure -----------------------------------------------------
mkdir -p \
  /opt/ldbc-snb/social_network \
  /opt/ldbc-snb/substitution_parameters \
  /opt/ldbc-snb/social_network_converted \
  /opt/ldbc-snb/results
chmod -R 777 /opt/ldbc-snb

# --- Clone and build impls repo ----------------------------------------------
git clone https://github.com/ldbc/ldbc_snb_interactive_v1_impls.git \
  /opt/ldbc-snb/ldbc_snb_interactive_v1_impls

cd /opt/ldbc-snb/ldbc_snb_interactive_v1_impls
mvn clean package -DskipTests --projects common,cypher --also-make

# --- Pre-configure benchmark.properties --------------------------------------
cat > /opt/ldbc-snb/ldbc_snb_interactive_v1_impls/cypher/driver/benchmark.properties << PROPS
endpoint=bolt://localhost:7687
user=neo4j
password=admin
queryDir=queries/
printQueryNames=false
printQueryStrings=false
printQueryResults=false
status=1
thread_count=${cores}
name=LDBC-SNB
mode=execute_benchmark
time_unit=MILLISECONDS
time_compression_ratio=0.1
peer_identifiers=
workload_statistics=false
spinner_wait_duration=1
help=false
ignore_scheduled_start_times=false
workload=org.ldbcouncil.snb.driver.workloads.interactive.LdbcSnbInteractiveWorkload
db=org.ldbcouncil.snb.impls.workloads.cypher.interactive.CypherInteractiveDb
warmup=1000
operation_count=3000
ldbc.snb.interactive.updates_dir=/opt/ldbc-snb/social_network/
ldbc.snb.interactive.parameters_dir=/opt/ldbc-snb/substitution_parameters/
ldbc.snb.interactive.short_read_dissipation=0.2
ldbc.snb.interactive.scale_factor=1
ldbc.snb.interactive.LdbcQuery1_enable=true
ldbc.snb.interactive.LdbcQuery2_enable=true
ldbc.snb.interactive.LdbcQuery3_enable=true
ldbc.snb.interactive.LdbcQuery4_enable=true
ldbc.snb.interactive.LdbcQuery5_enable=true
ldbc.snb.interactive.LdbcQuery6_enable=true
ldbc.snb.interactive.LdbcQuery7_enable=true
ldbc.snb.interactive.LdbcQuery8_enable=true
ldbc.snb.interactive.LdbcQuery9_enable=true
ldbc.snb.interactive.LdbcQuery10_enable=true
ldbc.snb.interactive.LdbcQuery11_enable=true
ldbc.snb.interactive.LdbcQuery12_enable=true
ldbc.snb.interactive.LdbcQuery13_enable=true
ldbc.snb.interactive.LdbcQuery14_enable=true
ldbc.snb.interactive.LdbcShortQuery1PersonProfile_enable=true
ldbc.snb.interactive.LdbcShortQuery2PersonPosts_enable=true
ldbc.snb.interactive.LdbcShortQuery3PersonFriends_enable=true
ldbc.snb.interactive.LdbcShortQuery4MessageContent_enable=true
ldbc.snb.interactive.LdbcShortQuery5MessageCreator_enable=true
ldbc.snb.interactive.LdbcShortQuery6MessageForum_enable=true
ldbc.snb.interactive.LdbcShortQuery7MessageReplies_enable=true
ldbc.snb.interactive.LdbcUpdate1AddPerson_enable=true
ldbc.snb.interactive.LdbcUpdate2AddPostLike_enable=true
ldbc.snb.interactive.LdbcUpdate3AddCommentLike_enable=true
ldbc.snb.interactive.LdbcUpdate4AddForum_enable=true
ldbc.snb.interactive.LdbcUpdate5AddForumMembership_enable=true
ldbc.snb.interactive.LdbcUpdate6AddPost_enable=true
ldbc.snb.interactive.LdbcUpdate7AddComment_enable=true
ldbc.snb.interactive.LdbcUpdate8AddFriendship_enable=true
PROPS

# --- Fetch benchmark scripts from instance metadata --------------------------
METADATA="http://metadata.google.internal/computeMetadata/v1/instance/attributes"

curl -sf "$METADATA/run-benchmark-script" -H "Metadata-Flavor: Google" \
  > /opt/ldbc-snb/run-benchmark.sh

curl -sf "$METADATA/setup-and-run-script" -H "Metadata-Flavor: Google" \
  > /opt/ldbc-snb/setup-and-run.sh

chmod +x /opt/ldbc-snb/run-benchmark.sh /opt/ldbc-snb/setup-and-run.sh
chmod -R 777 /opt/ldbc-snb

# --- VM info -----------------------------------------------------------------
cat > /opt/ldbc-snb/vm-info.txt << INFO
cores=$CORES
variant=$VARIANT
machine_type=n2d-standard-$CORES
neo4j_version=4.4.24
INFO

echo "Startup complete"
