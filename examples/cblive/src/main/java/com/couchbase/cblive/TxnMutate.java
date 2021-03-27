package com.couchbase.cblive;

import com.couchbase.client.core.cnc.Event;
import com.couchbase.client.core.error.DocumentNotFoundException;
import com.couchbase.client.java.Bucket;
import com.couchbase.client.java.Cluster;
import com.couchbase.client.java.Collection;
import com.couchbase.client.java.json.JsonObject;
import com.couchbase.transactions.TransactionDurabilityLevel;
import com.couchbase.transactions.TransactionGetResult;
import com.couchbase.transactions.Transactions;
import com.couchbase.transactions.config.TransactionConfigBuilder;
import com.couchbase.transactions.error.TransactionCommitAmbiguous;
import com.couchbase.transactions.error.TransactionFailed;
import com.couchbase.transactions.log.TransactionEvent;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.Duration;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

class InsufficientFunds extends RuntimeException {
}

class CustomerNotFound extends RuntimeException {
}

class TxnMutate {
	private static final Logger logger = LoggerFactory.getLogger(TxnMutate.class);

	public static void main(String[] args) {
		var clusterName = "couchbase://172.23.96.189";
		var username = "Administrator";
		var password = "password";
		var bucketName = "travel-sample";
		var verbose = true;
		var amount = 100;

		run(clusterName, username, password, bucketName, TransactionDurabilityLevel.NONE, verbose, amount);
		
		System.exit(0);
	}

	private static void run(String clusterName, String username, String password, String bucketName,
			TransactionDurabilityLevel transactionDurabilityLevel, boolean verbose, int amount) {

		// Initialize the Couchbase cluster
		Cluster cluster = Cluster.connect(clusterName, username, password);
		Bucket bucket = cluster.bucket(bucketName);
		Collection collection = bucket.defaultCollection();
		bucket.waitUntilReady(Duration.ofSeconds(30));

		// Create Transactions config
		TransactionConfigBuilder config = TransactionConfigBuilder.create().durabilityLevel(transactionDurabilityLevel);

		if (verbose) {
			config.logDirectly(Event.Severity.VERBOSE);
		}

		// Initialize transactions. Must only be one Transactions object per app as it
		// creates background resources.
		Transactions transactions = Transactions.create(cluster, config);

		// Optional but recommended - subscribe for events
		cluster.environment().eventBus().subscribe(event -> {
			if (event instanceof TransactionEvent) {

				TransactionEvent te = (TransactionEvent) event;

				if (te.severity().ordinal() >= Event.Severity.WARN.ordinal()) {
					// handle important event
				}
			}
		});

		// Setup test data
		JsonObject customer1 = JsonObject.create().put("type", "Customer").put("name", "Andy").put("balance", 100);

		JsonObject customer2 = JsonObject.create().put("type", "Customer").put("name", "Beth").put("balance", 100);

		collection.upsert("andy", customer1);

		logger.info("Upserted sample customer document " + customer1);

		collection.upsert("beth", customer2);

		logger.info("Upserted sample customer document " + customer2);

		try {
			transferMoney(transactions, collection, "andy", "beth", amount);
		} catch (RuntimeException err) {
			System.err.println("Transaction failed with: " + err.toString());
		}
	}

	private static void transferMoney(Transactions transactions, Collection collection, String customer1Id,
			String customer2Id, int amount) {
		// This shows how to pass values from the transaction lambda
		AtomicReference<String> transferId = new AtomicReference<>();

		try {

			// Supply transactional logic inside a lambda - any required retries are handled
			// for you
			transactions.run(ctx -> {

				// getOrError means "fail the transaction if that key does not exist"
				TransactionGetResult customer1 = ctx.get(collection, customer1Id);
				TransactionGetResult customer2 = ctx.get(collection, customer2Id);
				// Optional<TransactionJsonDocument> customer2Opt = ctx.get(collection,
				// customer2Id);

				JsonObject customer1Content = customer1.contentAsObject();
				JsonObject customer2Content = customer2.contentAsObject();

				logger.info("In transaction - got customer 1's details: " + customer1Content);
				logger.info("In transaction - got customer 2's details: " + customer2Content);

				int customer1Balance = customer1Content.getInt("balance");
				int customer2Balance = customer2Content.getInt("balance");

				// Create a record of the transfer
				JsonObject transferRecord = JsonObject.create().put("from", customer1Id).put("to", customer2Id)
						.put("amount", amount).put("type", "Transfer");
				// We want this value outside the transaction lambda, so pass it in an
				// AtomicReference
				transferId.set(UUID.randomUUID().toString());

				ctx.insert(collection, transferId.get(), transferRecord);

				logger.info("In transaction - creating record of transfer with UUID: " + transferId.get());

				if (customer1Balance >= amount) {
					logger.info("In transaction - customer 1 has sufficient balance, transferring " + amount);

					customer1Content.put("balance", customer1Balance - amount);
					customer2Content.put("balance", customer2Balance + amount);

					logger.info(
							"In transaction - changing customer 1's balance to: " + customer1Content.getInt("balance"));
					logger.info(
							"In transaction - changing customer 2's balance to: " + customer2Content.getInt("balance"));

					ctx.replace(customer1, customer1Content);
					ctx.replace(customer2, customer2Content);
				} else {
					logger.info("In transaction - customer 1 has insufficient balance to transfer " + amount);

					// Rollback is automatic on a thrown exception. This will also cause the
					// transaction to fail
					// with a TransactionFailed containing this InsufficientFunds as the getCause()
					// - see below.
					throw new InsufficientFunds();
				}

				// If we reach here, commit is automatic.
				logger.info("In transaction - about to commit");
				// ctx.commit(); // can also, and optionally, explicitly commit
			});
		} catch (TransactionCommitAmbiguous err) {
			System.err.println("Transaction " + err.result().transactionId() + " possibly committed:");
			err.result().log().logs().forEach(System.err::println);
		} catch (TransactionFailed err) {

			if (err.getCause() instanceof InsufficientFunds) {
				throw (RuntimeException) err.getCause(); // propagate up
			}
			// ctx.getOrError can raise a DocumentNotFoundException
			else if (err.getCause() instanceof DocumentNotFoundException) {
				throw new CustomerNotFound();
			} else {
				// Unexpected error - log for human review
				// This per-txn log allows the app to only log failures
				System.err.println("Transaction " + err.result().transactionId() + " did not reach commit:");

				err.result().log().logs().forEach(System.err::println);
			}
		}

		// Post-transaction, see the results:
		JsonObject customer1 = collection.get(customer1Id).contentAsObject();
		JsonObject customer2 = collection.get(customer2Id).contentAsObject();

		logger.info("After transaction - got customer 1's details: " + customer1);
		logger.info("After transaction - got customer 2's details: " + customer2);

		if (transferId.get() != null) {
			JsonObject transferRecord = collection.get(transferId.get()).contentAsObject();

			logger.info("After transaction - transfer record: " + transferRecord);
		}
	}
}
