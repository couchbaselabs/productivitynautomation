package com.couchbase.cblive;

import com.couchbase.client.core.cnc.Event;
import com.couchbase.client.core.error.DocumentNotFoundException;
import com.couchbase.client.java.Bucket;
import com.couchbase.client.java.Cluster;
import com.couchbase.client.java.Collection;
import com.couchbase.client.java.json.JsonArray;
import com.couchbase.client.java.json.JsonObject;
import com.couchbase.transactions.TransactionDurabilityLevel;
import com.couchbase.transactions.TransactionGetResult;
import com.couchbase.transactions.Transactions;
import com.couchbase.transactions.config.TransactionConfigBuilder;
import com.couchbase.transactions.error.TransactionCommitAmbiguous;
import com.couchbase.transactions.error.TransactionFailed;
import com.couchbase.transactions.log.TransactionEvent;

import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

class NoRouteFound extends RuntimeException {
	private static final long serialVersionUID = 1L;
}

class TxnMutate2 {
	public static void main(String[] args) {
		var clusterName = "couchbase://172.23.96.189";
		var username = "Administrator";
		var password = "password";
		var bucketName = "travel-sample";

		Cluster cluster = Cluster.connect(clusterName, username, password);
		Bucket bucket = cluster.bucket(bucketName);
		Collection collection = bucket.defaultCollection();
		bucket.waitUntilReady(Duration.ofSeconds(30));

		TransactionConfigBuilder config = TransactionConfigBuilder.create()
				.durabilityLevel(TransactionDurabilityLevel.NONE);
		Transactions transactions = Transactions.create(cluster, config);
		cluster.environment().eventBus().subscribe(event -> {
			if (event instanceof TransactionEvent) {
				TransactionEvent te = (TransactionEvent) event;
				if (te.severity().ordinal() >= Event.Severity.WARN.ordinal()) {
					System.out.println("Handling of the transaction event...");
				}
			}
		});

		var route1Id = "route_46586";
		var route2Id = "route_35816";

		// Pre-transaction
		JsonObject preroute1 = collection.get(route1Id).contentAsObject();
		JsonObject preroute2 = collection.get(route2Id).contentAsObject();

		System.out.printf(
				"Before transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
				route1Id, preroute1.getString("sourceairport"), preroute1.getString("destinationairport"),
				preroute1.getString("airline"), preroute1.getString("airlineid"));
		System.out.printf(
				"Before transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
				route2Id, preroute2.getString("sourceairport"), preroute2.getString("destinationairport"),
				preroute2.getString("airline"), preroute2.getString("airlineid"));

		// In-transaction
		try {
			String swapId = swapAirlineRoute(transactions, collection, route1Id, route2Id);
			if (swapId != null) {
				JsonObject swapRecord = collection.get(swapId).contentAsObject();
				System.out.println("After transaction - swap route record: " + swapRecord);
			}
		} catch (RuntimeException err) {
			System.err.println("Transaction failed with: " + err.toString());
		}

		// Post-transaction
		JsonObject postroute1 = collection.get(route1Id).contentAsObject();
		JsonObject postroute2 = collection.get(route2Id).contentAsObject();

		System.out.printf(
				"After transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
				route1Id, postroute1.getString("sourceairport"), postroute1.getString("destinationairport"),
				postroute1.getString("airline"), postroute1.getString("airlineid"));
		System.out.printf(
				"After transaction - got %s's details: source airport: %s, destination airport: %s, airline: %s:%s \n",
				route2Id, postroute2.getString("sourceairport"), postroute2.getString("destinationairport"),
				postroute2.getString("airline"), postroute2.getString("airlineid"));

		System.exit(0);
	}

	private static String swapAirlineRoute(Transactions transactions, Collection collection, String route1Id,
			String route2Id) {
		AtomicReference<String> swapId = new AtomicReference<>();
		try {
			transactions.run(ctx -> {
				TransactionGetResult route1 = ctx.get(collection, route1Id);
				TransactionGetResult route2 = ctx.get(collection, route2Id);

				JsonObject route1Content = route1.contentAsObject();
				JsonObject route2Content = route2.contentAsObject();

				System.out.printf("In transaction - got %s's details: %s\n", route1Id,
						route1Content.getString("airlineid"));
				System.out.printf("In transaction - got %s's details: %s\n", route2Id,
						route2Content.getString("airlineid"));

				String airlineid1 = route1Content.getString("airlineid");
				String airline1 = route1Content.getString("airline");
				JsonArray schedule1 = route1Content.getArray("schedule");
				String airline2 = route2Content.getString("airline");
				String airlineid2 = route2Content.getString("airlineid");
				JsonArray schedule2 = route2Content.getArray("schedule");

				JsonObject swapRecord = JsonObject.create().put("from_route", route1Id).put("to_route", route2Id)
						.put("from_airline", airline1).put("from_airlineid", airlineid1).put("to_airline", airline2)
						.put("to_airlineid", airlineid2).put("type", "Transactions_History")
						.put("created", new java.util.Date().toString());
				swapId.set(UUID.randomUUID().toString());

				ctx.insert(collection, swapId.get(), swapRecord);

				System.out.println("In transaction - creating a record of swap routes with UUID: " + swapId.get());

				route1Content.put("airline", airline2);
				route1Content.put("airlineid", airlineid2);
				route1Content.put("schedule", schedule2);
				route2Content.put("airline", airline1);
				route2Content.put("airlineid", airlineid1);
				route2Content.put("schedule", schedule1);

				System.out.printf("In transaction - changing %s's airline to: %s\n", route1Id,
						route1Content.getString("airlineid"));
				System.out.printf("In transaction - changing %s's airline to: %s\n", route2Id,
						route2Content.getString("airlineid"));

				ctx.replace(route1, route1Content);
				ctx.replace(route2, route2Content);

				System.out.println("In transaction - about to commit");
				ctx.commit();
			});
		} catch (TransactionCommitAmbiguous err) {
			System.err.println("Transaction " + err.result().transactionId() + " possibly committed:");
			err.result().log().logs().forEach(System.err::println);
		} catch (TransactionFailed err) {
			if (err.getCause() instanceof DocumentNotFoundException) {
				throw new NoRouteFound();
			} else {
				System.err.println("Transaction " + err.result().transactionId() + " did not reach commit:");
				err.result().log().logs().forEach(System.err::println);
			}
		}
		return swapId.get();

	}
}
